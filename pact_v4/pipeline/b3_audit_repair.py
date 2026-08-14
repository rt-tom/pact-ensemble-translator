"""B3: production audit/repair pipeline (concept §10 B3 + §9.4).

Wires the B-series components into one orchestrator the strict runner can
call after whole-chapter generation:

    1. entity context (B1.2, optional, ``entity_context_enabled``) —
       ``extract_entity_context(source)`` -> per-chapter cache
       (``source_hash + extractor_version``), rendered into the audit
       prompt's ``CHAPTER ENTITY FACTS`` block and passed to the hard
       filters (entity-PID issues are forced to TIER_B, §5.3);
    2. chunked audit (B1) — ``ChunkedAuditEvaluator`` over the whole
       chapter pairs (narrator + entity blocks), replacing the old
       ``gemma_russian_review`` / ``qwen_fidelity`` gates;
    3. gate: ``audit_complete == False`` -> FAIL-CLOSED (no repair, the
       chapter is never released as passed audit);
    4. hard filters (B1.1) — ``apply_hard_filters`` -> CONFIRMED /
       REJECTED / TIER_B;
    5. selective repair (B2) — ``SelectiveRepairEvaluator`` (Tier A
       direct, Tier B verify-before-repair) with its single post-repair
       re-audit of the changed PIDs;
    6. ``translations_repaired`` = raw map + committed repairs.

Provenance / cache contract:

* ``audit_journal.ndjson`` (schema ``pact-v4-b3-audit-journal/v1``) —
  append-only events ``audit_started`` / ``audit_chunk_started`` /
  ``audit_chunk_done`` / ``audit_complete`` / ``audit_failed`` (terminal
  pre/model-call evaluator failure — always followed by a fail-closed
  ``gate``) / ``finding`` / ``repair_round`` / ``reaudit_scope`` /
  ``gate``. This is a SEPARATE
  file from the generation ``journal.ndjson``: the whole-chapter resume
  contract requires exactly one generation entry, so audit events can
  never share that file.
* ``audit_cache_b3.json`` (schema ``pact-v4-b3-audit-cache/v1``) — audit
  cache identity = ``snapshot_hash + translation_hash + config_identity +
  backend_identity_hash + prompt_version + harness_version +
  entity_context_hash`` (the card's identity, plus the exact translation
  content — the audit outcome is a function of both source and translation;
  entity hash present only when entity context is enabled). A full cache
  hit reuses the stored outcome (0 model calls). A cached
  ``audit_complete=False`` with an intact identity is a PARTIAL hit
  (PARTIAL-RESUME t_a58dd881): GOOD/GOOD_RETRIED audit chunks and GOOD
  R-editor chunks are replayed with 0 model calls (their stored issues /
  parse-validated edits reused verbatim), the TRANSPORT_ERROR/EMPTY/FAILED
  chunks are re-run; the fail-closed gate stays — the chapter is never
  released as passed audit until EVERY chunk has a status, a cache is never
  downgraded, and an identity mismatch is still a full miss.
* ``entity_context_cache.json`` (schema from ``entity_extractor``) — the
  B1.2 per-chapter entity cache (``source_hash + extractor_version``).
* ``entity_context_validation_report.json`` (schema
  ``pact-v4-entity-extractor-validation/v1``) — B3-DIAG transparency:
  what the model PROPOSED vs what the code ACCEPTED. Written next to the
  entity cache whenever a FRESH extraction ran validation (drop/downgrade
  decisions with reasons from ``EntityExtractionResult.validation``). A
  cache hit reuses the previously validated context and never clobbers the
  original report; an extractor failure (never reaching validation) writes
  neither the cache nor the report.

Transport: audit/repair/entity calls go through ``CompletionBackend``
(``build_role_backend``), so the same pipeline serves local, remote and
composite profiles. Remote audit through ``opencode serve`` is a
CONTRACT, NOT tested yet (owner decision: test remote audit after the
B-phase; the evaluators never emit ``request_options`` — the reasoning
budget is a server arg).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, AbstractSet

from pact_v4.audit.chunked_audit import (
    AUDIT_V4_CATEGORIES,
    AUDIT_V4_CONFIDENCES,
    AUDIT_V4_SEVERITIES,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_REASONING_BUDGET,
    DEFAULT_TRANSPORT_MAX_RETRIES,
    DEFAULT_TRANSPORT_BASE_DELAY_SECONDS,
    HARNESS_VERSION,
    PROMPT_VERSION,
    AuditPair,
    ChunkedAuditConfig,
    ChunkedAuditEvaluator,
    ChunkedAuditOutcome,
    build_narrator_context,
    pairs_from_maps,
)
from pact_v4.audit.entity_extractor import (
    EXTRACTOR_VERSION,
    BackendEntityExtractor,
    BackendEntityExtractorConfig,
    ChapterEntityContext,
    EntityContextCache,
    EntityExtractionResult,
    extract_entity_context,
)
from pact_v4.audit.hard_filters import FilteredIssue, apply_hard_filters
from pact_v4.audit.russian_editor import (
    RUSSIAN_EDITOR_HARNESS_VERSION,
    RUSSIAN_EDITOR_PROMPT_VERSION,
    SAFE_CLASSES,
    ALL_CLASSES,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_TOKENS as RUSSIAN_EDITOR_DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_PAIRS,
    MAX_EDITS_PER_PID,
    DEFAULT_RETRY_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_SECONDS,
    ReviewCandidate,
    RussianEditorConfig,
    RussianEditorEvaluator,
    RussianEditorOutcome,
)
from pact_v4.phase1.models import SourceArtifact, canonical_json_hash
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
    REAUDIT_DELTA_FORMAT,
    MICROBATCH_TARGET,
    MICROBATCH_TRIGGER,
    REPAIR_FINDINGS_CAP,
    REPAIR_HARNESS_VERSION,
    REPAIR_PROMPT_VERSION,
    SelectiveRepairConfig,
    SelectiveRepairEvaluator,
    SelectiveRepairOutcome,
)
from pact_v4.runtime.backend_protocol import (
    BackendDescriptor,
    CompletionBackend,
    CompletionRequest,
)
from pact_v4.runtime.json_resilience import JsonRetryPolicy

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Artifact schemas (identity-bearing, never reused across a schema change)
# ---------------------------------------------------------------------------

B3_AUDIT_CACHE_SCHEMA = "pact-v4-b3-audit-cache/v1"
B3_AUDIT_JOURNAL_SCHEMA = "pact-v4-b3-audit-journal/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomic write (write-then-rename) with a UTF-8 JSON payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Entity-context block renderer (deterministic, source-derived)
# ---------------------------------------------------------------------------


def render_entity_context_block(context: ChapterEntityContext) -> str:
    """Render a validated ``ChapterEntityContext`` into the audit prompt's
    ``CHAPTER ENTITY FACTS - SOURCE-DERIVED`` block.

    Deterministic (sorted by entity name); the block is data for the
    auditor (evidence level 3: source > adjacent > chapter facts), never
    an instruction. Empty context -> empty string (caller omits the
    block).
    """
    if not context.entities:
        return ""
    lines: list = []
    for record in sorted(context.entities, key=lambda r: r.entity):
        lines.append(f"- entity: {record.entity}")
        lines.append(f"  established_type: {record.canonical_type}")
        anchor = record.anchor
        lines.append(
            f"  anchor: \"{anchor.span}\" (pid {anchor.pid}, {anchor.status})"
        )
        for alias in record.aliases:
            lines.append(
                f"  alias: \"{alias.surface}\" (pid {alias.pid}, {alias.status})"
            )
        for claim in record.claims:
            evidence = ", ".join(
                f"{ev.pid} \"{ev.span}\"" for ev in claim.evidence
            )
            windows = ", ".join(
                f"[{a}-{b}]" for a, b in claim.evidence_windows
            )
            detail = f"evidence: {evidence}" if evidence else ""
            if windows:
                detail += f" windows: {windows}"
            lines.append(
                f"  claim: {claim.kind}={claim.value!r} ({claim.status})"
                + (f" {detail}" if detail else "")
            )
    return "\n".join(lines) + "\n"


def render_entity_context_to_hard_filters(
    context: ChapterEntityContext,
) -> Mapping[str, Any]:
    """Payload form for ``apply_hard_filters`` (the ``entities`` list)."""
    return context.to_payload()


# ---------------------------------------------------------------------------
# Audit journal (append-only, one event per line, crash-safe)
# ---------------------------------------------------------------------------


class AuditJournal:
    """Append-only audit provenance journal (write-only, one line/event).

    Deliberately separate from the generation ``journal.ndjson``: the
    whole-chapter resume contract requires exactly one generation entry,
    so audit events are recorded here. A write failure disables the
    writer and is logged, never raised — provenance must not break a run.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Optional[Any] = None
        self._disabled = False

    def _ensure_open(self) -> None:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        if self._disabled:
            return
        record = {
            "schema": B3_AUDIT_JOURNAL_SCHEMA,
            "event": event,
            "ts": _now_iso(),
            **fields,
        }
        try:
            self._ensure_open()
            assert self._handle is not None
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()
        except OSError as exc:
            LOG.warning("B3 audit journal write failed (%s); disabling", exc)
            self._disabled = True

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


# ---------------------------------------------------------------------------
# Config / result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class B3AuditRepairConfig:
    """Settings for one B3 audit/repair pass (frozen contract of the run).

    ``entity_context_enabled`` (runtime config, default True — owner
    decision 2026-08-10, B1.3 gate pending): True runs the source-only
    entity prepass and feeds both the auditor and the hard filters; False
    audits without the entity block. The audit input params
    (``max_input_tokens``/``max_tokens``/``overlap_tokens``) are part of
    the run config identity via ``StrictRunConfig`` and of the audit
    cache identity via the stored payload.
    """

    entity_context_enabled: bool = True
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS
    reasoning_budget: int = DEFAULT_REASONING_BUDGET
    # R-RETRY (t_8ab8ab35, operator extension 2026-08-13): the chunk-level
    # TRANSPORT_ERROR bounded retry policy (NEW session per attempt). It is
    # part of the run config identity (StrictRunConfig.to_config_artifact)
    # and is wired into ChunkedAuditConfig below — a cache written under a
    # different transport-retry policy must never replay a failed chunk.
    audit_transport_max_retries: int = DEFAULT_TRANSPORT_MAX_RETRIES
    audit_transport_base_delay_seconds: float = DEFAULT_TRANSPORT_BASE_DELAY_SECONDS
    repair_findings_cap: int = REPAIR_FINDINGS_CAP
    repair_microbatch_trigger: int = MICROBATCH_TRIGGER
    repair_microbatch_target: int = MICROBATCH_TARGET
    # REPAIR-CTX (card t_97b31f81, F5): the repair-batch local context
    # window (±N neighbour pairs around each finding PID) is part of the run
    # config identity (StrictRunConfig.to_config_artifact) and is wired into
    # SelectiveRepairConfig below — a window change must invalidate a stale
    # cached repaired map (old full-chapter batches can never replay under a
    # local-context prompt).
    repair_context_window: int = DEFAULT_REPAIR_CONTEXT_WINDOW
    # REPAIR-2 (card t_768537b9, F5): the per-category window overrides
    # ({category: window}; categories not in the map fall back to
    # ``repair_context_window`` — invented_gender/referent/omission default
    # ±10, changed_fact/addition stay ±3). Part of the run config identity
    # and wired into SelectiveRepairConfig below — a window change must
    # invalidate a stale cached repaired map.
    repair_context_window_by_category: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY)
    )
    repair_reaudit_neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW
    # REPAIR-CTX (t_97b31f81, owner decision 2026-08-12): the re-audit is a
    # CHUNKED audit over the affected region (changed PIDs + neighbours) —
    # the whole-chapter re-audit mode (old full_threshold) is CANCELLED. The
    # re-audit chunk/overlap settings and the REPAIRED CHANGES delta format
    # are identity-bearing (F5): a change invalidates a stale cached repaired
    # map (an old full-chapter re-audit can never replay under the chunked
    # local-context prompt).
    repair_reaudit_max_input_tokens: int = DEFAULT_REAUDIT_MAX_INPUT_TOKENS
    repair_reaudit_overlap_tokens: int = DEFAULT_REAUDIT_OVERLAP_TOKENS
    repair_reaudit_min_overlap_pairs: int = DEFAULT_REAUDIT_MIN_OVERLAP_PAIRS
    repair_reaudit_max_overlap_pairs: int = DEFAULT_REAUDIT_MAX_OVERLAP_PAIRS
    repair_reaudit_delta_format: str = REAUDIT_DELTA_FORMAT
    # The re-audit output budget and its bounded B4 JSON retry policy are
    # part of the run config identity (StrictRunConfig.to_config_artifact)
    # and are wired into SelectiveRepairConfig below (RV 71b7cbc fix, F5) —
    # without them the re-audit would silently fall back to module defaults
    # and a cache written under a different budget/policy could replay.
    repair_reaudit_max_tokens: int = DEFAULT_REAUDIT_MAX_TOKENS
    repair_reaudit_max_retries: int = DEFAULT_REAUDIT_MAX_RETRIES
    repair_reaudit_base_delay_seconds: float = DEFAULT_REAUDIT_BASE_DELAY_SECONDS
    prompt_version: str = PROMPT_VERSION
    harness_version: str = HARNESS_VERSION
    extractor_version: str = EXTRACTOR_VERSION
    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR prompt
    # version (REPAIR_AS_VERIFIER_V1 v4 — the source_stage/merge contract)
    # is identity-bearing and wired into SelectiveRepairConfig below (F5) —
    # a cache written under a different repair prompt must never replay the
    # repaired map. Defaults mirror the module constants and are overridden
    # by _build_b3_audit_repair from StrictRunConfig.audit_repair_prompt_version.
    repair_prompt_version: str = REPAIR_PROMPT_VERSION
    repair_harness_version: str = REPAIR_HARNESS_VERSION
    # V4.2 R (card t_4707e6e5): Russian-only editor stage BEFORE the audit.
    # ``russian_editor_enabled`` is True by default (owner decision
    # 2026-08-11 — R is on unless the CLI disables it with
    # ``--no-russian-editor``, scheme 4.1). The R settings are identity-
    # bearing via ``StrictRunConfig`` (F5 lesson): russian_editor_version +
    # chunk settings + class threshold are part of the run config identity,
    # so flipping any of them invalidates the repaired cache — an old
    # repaired map can never replay under a different editor policy.
    russian_editor_enabled: bool = True
    russian_editor_version: str = RUSSIAN_EDITOR_PROMPT_VERSION
    russian_editor_harness_version: str = RUSSIAN_EDITOR_HARNESS_VERSION
    russian_editor_chunk_size: int = DEFAULT_CHUNK_SIZE
    russian_editor_overlap_pairs: int = DEFAULT_OVERLAP_PAIRS
    russian_editor_max_tokens: int = RUSSIAN_EDITOR_DEFAULT_MAX_TOKENS
    # Class threshold: classes in this frozenset are SAFE (auto-applied with
    # the diff-gate); every other known class routes to REVIEW candidates.
    russian_editor_safe_classes: frozenset = frozenset(SAFE_CLASSES)
    # R-RETRY (t_8ab8ab35, F5): the per-pid edit cap (duplicate pid is NOT
    # an error — up to this many edits per pid; 11th+ drops per-edit with a
    # WARNING) and the bounded retry policy (transport + empty/truncated
    # JSON) are identity-bearing — a cache written under a different
    # cap/retry policy must never replay the edited map.
    russian_editor_max_edits_per_pid: int = MAX_EDITS_PER_PID
    russian_editor_retry_max_retries: int = DEFAULT_RETRY_MAX_RETRIES
    russian_editor_retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS

    def to_payload(self) -> Dict[str, Any]:
        return {
            "entity_context_enabled": self.entity_context_enabled,
            "max_input_tokens": self.max_input_tokens,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "reasoning_budget": self.reasoning_budget,
            "audit_transport_retry": {
                "max_retries": self.audit_transport_max_retries,
                "base_delay_seconds": self.audit_transport_base_delay_seconds,
            },
            "repair_findings_cap": self.repair_findings_cap,
            "repair_microbatch_trigger": self.repair_microbatch_trigger,
            "repair_microbatch_target": self.repair_microbatch_target,
            "repair_context_window": self.repair_context_window,
            "repair_context_window_by_category": dict(
                self.repair_context_window_by_category
            ),
            "repair_reaudit_neighbour_window": self.repair_reaudit_neighbour_window,
            "repair_reaudit_chunk": {
                "max_input_tokens": self.repair_reaudit_max_input_tokens,
                "overlap_tokens": self.repair_reaudit_overlap_tokens,
                "min_overlap_pairs": self.repair_reaudit_min_overlap_pairs,
                "max_overlap_pairs": self.repair_reaudit_max_overlap_pairs,
                "delta_format": self.repair_reaudit_delta_format,
            },
            "repair_reaudit_max_tokens": self.repair_reaudit_max_tokens,
            "repair_reaudit_retry": {
                "max_retries": self.repair_reaudit_max_retries,
                "base_delay_seconds": self.repair_reaudit_base_delay_seconds,
            },
            "prompt_version": self.prompt_version,
            "harness_version": self.harness_version,
            "extractor_version": self.extractor_version,
            # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR
            # prompt version rides the payload/report like every other
            # version field (F5 — the repaired map is a function of it).
            "repair_prompt_version": self.repair_prompt_version,
            "repair_harness_version": self.repair_harness_version,
            "russian_editor": {
                "enabled": self.russian_editor_enabled,
                "version": self.russian_editor_version,
                "harness_version": self.russian_editor_harness_version,
                "chunk_size": self.russian_editor_chunk_size,
                "overlap_pairs": self.russian_editor_overlap_pairs,
                "max_tokens": self.russian_editor_max_tokens,
                "safe_classes": sorted(self.russian_editor_safe_classes),
                "max_edits_per_pid": self.russian_editor_max_edits_per_pid,
                "r_editor_retry": {
                    "max_retries": self.russian_editor_retry_max_retries,
                    "base_delay_seconds": self.russian_editor_retry_base_delay_seconds,
                },
            },
        }


@dataclass(frozen=True)
class B3AuditRepairResult:
    """Aggregated result of one B3 pass (feeds the run record)."""

    step6: Dict[str, Any]  # audit summary
    step7: Dict[str, Any]  # repair summary
    step8: Dict[str, Any]  # gate / terminal
    translations_repaired: Dict[str, str]
    audit_complete: bool
    from_cache: bool
    entity_context_hash: Optional[str]
    audit_cache_path: Path
    journal_path: Path
    # V4.2 R: Russian-editor stage report (None when R is disabled) — the
    # runner records it in the trial record (edit_candidates + accept/reject
    # journal); on a cache hit it is restored from the audit cache payload.
    r_editor: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Entity-extractor backend view (role resolution without identity change)
# ---------------------------------------------------------------------------


class _EntityRoleView:
    """CompletionBackend view presenting an ``entity_extractor`` binding.

    The local/remote descriptors do not carry an ``entity_extractor``
    role; ``BackendEntityExtractor`` needs one to resolve its model ref.
    This view adds the role pointing at the audit (Qwen) ref WITHOUT
    touching the real backend's identity — the descriptor is only used
    for role resolution, never for cache/resume identity (the run records
    ``cfg.backend.identity_hash``).
    """

    def __init__(self, backend: CompletionBackend, entity_ref: str) -> None:
        self._backend = backend
        self._entity_ref = entity_ref

    @property
    def descriptor(self) -> BackendDescriptor:
        base = self._backend.descriptor
        bindings = dict(base.model_bindings)
        bindings["entity_extractor"] = self._entity_ref
        bindings.setdefault("default", self._entity_ref)
        return replace(base, model_bindings=bindings)

    def complete(self, request: CompletionRequest) -> Any:
        return self._backend.complete(request)

    def close(self) -> None:
        self._backend.close()

    def call_records(self) -> Sequence[Any]:
        return self._backend.call_records()


# ---------------------------------------------------------------------------
# Audit cache (resume identity)
# ---------------------------------------------------------------------------


def _audit_cache_path(out_dir: Path) -> Path:
    return out_dir / "audit_cache_b3.json"


def _entity_cache_path(out_dir: Path) -> Path:
    return out_dir / "entity_context_cache.json"


def _entity_validation_report_path(out_dir: Path) -> Path:
    return out_dir / "entity_context_validation_report.json"


def _journal_path(out_dir: Path) -> Path:
    return out_dir / "audit_journal.ndjson"


def _load_entity_cache(out_dir: Path) -> EntityContextCache:
    path = _entity_cache_path(out_dir)
    if not path.exists():
        return EntityContextCache()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EntityContextCache.from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        # F6 (B3 review): a structurally corrupt dependent cache (JSON
        # list/object with missing keys, wrong entry shape, foreign schema)
        # can raise KeyError/TypeError/AttributeError deep inside
        # from_payload — NOT just OSError/ValueError. Any such failure is a
        # cache MISS (fail-closed): discard and recompute, never abort B3.
        LOG.warning(
            "B3: entity_context_cache.json unreadable/foreign (%s: %s); "
            "starting a fresh cache",
            type(exc).__name__, exc,
        )
        return EntityContextCache()


def _save_entity_cache(out_dir: Path, cache: EntityContextCache) -> None:
    _atomic_write_json(_entity_cache_path(out_dir), cache.to_payload())


def _save_entity_validation_report(
    out_dir: Path, report: Mapping[str, Any]
) -> None:
    """Persist the extractor's drop/downgrade decisions (B3-DIAG).

    Written next to ``entity_context_cache.json`` so a run shows what the
    model PROPOSED vs what the code ACCEPTED (dead PID / non-verbatim span /
    translation-derived / gender without referent link / relation
    verified->candidate). Only written when a fresh validation actually ran
    — a cache hit reuses the previously validated context, so the report of
    the original extraction is preserved, never clobbered with an empty one.
    """
    _atomic_write_json(
        _entity_validation_report_path(out_dir), report
    )


# ---------------------------------------------------------------------------
# PARTIAL-RESUME integrity (t_ec6bb8bc): the partial cache replay payload
# (chunks / issues / r_editor) is validated AND bound to a canonical
# integrity hash BEFORE any resume plan is built. A cache can preserve
# identity + translations_repaired_hash while its GOOD-chunk evidence or
# R edits are altered; without this gate resume would publish tampered
# payloads with 0 model calls. Any malformed / missing / duplicate /
# extra / mismatched partial payload is a FULL cache miss (recompute) —
# never a partial replay (fail-closed).
# ---------------------------------------------------------------------------


# Statuses the chunked audit can persist per chunk (classify_chunk + the
# TRANSPORT_ERROR / retry-shrink paths — see chunked_audit.py).
_AUDIT_CHUNK_STATUSES = frozenset({
    "GOOD", "GOOD_RETRIED", "FAILED_RETRIED", "TRANSPORT_ERROR",
    "LENGTH", "EMPTY", "SPILL", "INVALID_JSON",
})

# Statuses the Russian editor can persist per chunk (russian_editor.py).
_R_EDITOR_CHUNK_STATUSES = frozenset({"GOOD", "FAILED"})

# Exact persisted key set of an audit issue record (chunked_audit.py
# collection + ``_attach_debug_and_dedupe``). A record with a missing or
# extra key is a schema violation — never tolerated (RV2-A t_c84b6f13).
_ISSUE_KEYS = frozenset({
    "id", "category", "severity", "confidence", "note", "excerpt", "_debug",
})

# Report-level statuses produced by _build_r_editor_report (b3_audit_repair.py).
_R_EDITOR_REPORT_STATUSES = frozenset({
    "disabled", "failed", "partial", "incomplete", "complete",
})

# Statuses under which the R stage actually RAN (outcome REQUIRED).
_R_EDITOR_RAN_STATUSES = frozenset({"partial", "incomplete", "complete"})

# Exact persisted key sets (RussianEditorOutcome chunk records / edit
# payloads). A record with a foreign key is a schema violation — never
# tolerated, never coerced.
_R_CHUNK_KEYS = frozenset({"chunk", "first_pid", "last_pid", "status", "edits"})
_R_EDIT_KEYS = frozenset({"pid", "original", "rewritten", "reason", "class"})

def _validate_partial_payload(
    payload: Mapping[str, Any],
    *,
    expected_pids: Optional[Sequence[str]],
    current_text: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Validate the complete PARTIAL-RESUME replay payload.

    Runs inside ``B3AuditCache.load`` BEFORE either resume plan is
    constructed (audit_resume_plan / r_editor_resume_plan). Returns None
    when the payload is valid, or a reason string naming the first
    violation. Checks, fail-closed:

    * exact audit chunk coverage/order (contiguous 1..N, no duplicate /
      missing / extra indices) and a known status per chunk;
    * audit chunk boundaries (first_pid/last_pid strings) and pair counts
      (+ the int fields the evaluator replays verbatim);
    * issue schema (exact key set, non-empty string id/note/excerpt,
      category/severity/confidence vocab, PID membership in the
      translation map), ``_debug.chunk`` attribution inside the covered
      chunk indices, and PID membership inside the ACTUAL covered chunk
      span of the attributed chunk (first_pid..last_pid boundaries);
    * R report schema: when the R stage RAN (status partial/incomplete/
      complete) the ``outcome`` is REQUIRED with a non-empty, coherently
      tiled ``chunks`` list (contiguous 1..M, chunk_size-boundary
      coverage); when R was disabled/failed the ``outcome`` must be null —
      a malformed/missing R outcome is a full miss, never a partial replay
      while audit issues stay reusable;
    * every cached R edit: object shape, exact string types, PID
      membership (in the translation map AND inside the chunk's own
      boundary span), known class, and (when ``current_text`` is
      available) the verbatim current-text substring constraint.

    Malformed fields are NEVER stringified/coerced — a violation is a full
    cache miss. ``expected_pids`` / ``current_text`` may be None only when
    the caller has no map (PID-membership / text-substring checks are then
    skipped; the canonical hash below still binds the whole payload).
    """
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return "chunks is missing, not a list, or empty"
    expected_pid_set = frozenset(expected_pids) if expected_pids is not None else None
    for position, item in enumerate(chunks, start=1):
        if not isinstance(item, dict):
            return f"chunk at position {position}: not an object"
        chunk_index = item.get("chunk")
        if chunk_index != position:
            return (
                f"chunk coverage/order mismatch: stored index {chunk_index!r} "
                f"at position {position} (expected contiguous 1..N)"
            )
        status = item.get("status")
        if not isinstance(status, str) or status not in _AUDIT_CHUNK_STATUSES:
            return f"chunk {chunk_index}: unknown status {status!r}"
        first_pid = item.get("first_pid")
        last_pid = item.get("last_pid")
        if not isinstance(first_pid, str) or not first_pid:
            return f"chunk {chunk_index}: first_pid is missing or not a string"
        if not isinstance(last_pid, str) or not last_pid:
            return f"chunk {chunk_index}: last_pid is missing or not a string"
        if expected_pid_set is not None and (
            first_pid not in expected_pid_set or last_pid not in expected_pid_set
        ):
            return (
                f"chunk {chunk_index}: boundary pids {first_pid!r}/{last_pid!r} "
                f"are not in the translation map"
            )
        pair_count = item.get("pair_count")
        if not isinstance(pair_count, int) or isinstance(pair_count, bool) or pair_count < 1:
            return f"chunk {chunk_index}: pair_count {pair_count!r} is not a positive int"
        context_count = item.get("context_count")
        if not isinstance(context_count, int) or isinstance(context_count, bool) or context_count < 0:
            return f"chunk {chunk_index}: context_count {context_count!r} is not a non-negative int"
        reasoning_chars = item.get("reasoning_chars")
        if not isinstance(reasoning_chars, int) or isinstance(reasoning_chars, bool) or reasoning_chars < 0:
            return (
                f"chunk {chunk_index}: reasoning_chars {reasoning_chars!r} "
                f"is not a non-negative int"
            )
        if not isinstance(item.get("reasoning_file"), str):
            return f"chunk {chunk_index}: reasoning_file is not a string"
        finish_reason = item.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            return f"chunk {chunk_index}: finish_reason is not a string or null"

    issues = payload.get("issues")
    if not isinstance(issues, list):
        return "issues is not a list"
    chunk_count = len(chunks)
    # RV2-A (t_c84b6f13): a position map over the ordered PID list lets the
    # validator enforce that every issue sits inside the ACTUAL pid span of
    # the chunk it claims (_debug.chunk), not merely inside the global
    # translation map.
    pid_positions = None
    if expected_pids is not None:
        pid_positions = {pid: position for position, pid in enumerate(expected_pids)}
    for issue_index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            return f"issue {issue_index}: not an object"
        if set(issue) != _ISSUE_KEYS:
            return (
                f"issue {issue_index}: key set mismatch "
                f"(expected {sorted(_ISSUE_KEYS)}, got {sorted(issue)})"
            )
        pid = issue.get("id")
        if not isinstance(pid, str) or not pid:
            return f"issue {issue_index}: id is missing or not a string"
        if expected_pid_set is not None and pid not in expected_pid_set:
            return f"issue {issue_index}: id {pid!r} is not in the translation map"
        if issue.get("category") not in AUDIT_V4_CATEGORIES:
            return f"issue {pid}: invalid category {issue.get('category')!r}"
        if issue.get("severity") not in AUDIT_V4_SEVERITIES:
            return f"issue {pid}: invalid severity {issue.get('severity')!r}"
        if issue.get("confidence") not in AUDIT_V4_CONFIDENCES:
            return f"issue {pid}: invalid confidence {issue.get('confidence')!r}"
        note = issue.get("note")
        if not isinstance(note, str) or not note.strip():
            return f"issue {pid}: note is missing, not a string, or empty"
        excerpt = issue.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt.strip():
            return f"issue {pid}: excerpt is missing, not a string, or empty"
        debug = issue.get("_debug")
        if not isinstance(debug, dict):
            return f"issue {pid}: _debug is missing or not an object"
        chunk = debug.get("chunk")
        if not isinstance(chunk, int) or isinstance(chunk, bool) or not (1 <= chunk <= chunk_count):
            return (
                f"issue {pid}: _debug.chunk {chunk!r} is not inside the "
                f"covered chunk indices 1..{chunk_count}"
            )
        if not isinstance(debug.get("reasoning_file"), str):
            return f"issue {pid}: _debug.reasoning_file is not a string"
        if pid_positions is not None:
            chunk_record = chunks[chunk - 1]
            first_pos = pid_positions.get(chunk_record.get("first_pid"))
            last_pos = pid_positions.get(chunk_record.get("last_pid"))
            pid_pos = pid_positions.get(pid)
            if (
                first_pos is None or last_pos is None or pid_pos is None
                or not (first_pos <= pid_pos <= last_pos)
            ):
                return (
                    f"issue {pid}: id is not inside the actual pid span of "
                    f"chunk {chunk} ({chunk_record.get('first_pid')!r}.."
                    f"{chunk_record.get('last_pid')!r})"
                )

    r_editor = payload.get("r_editor")
    if r_editor is not None and not isinstance(r_editor, dict):
        return "r_editor is not an object or null"
    if isinstance(r_editor, dict):
        # FIX RV2-B (t_aa3ee032): the R report is validated COMPLETELY before
        # either resume plan is built. When the R stage RAN (status partial/
        # incomplete/complete) the outcome is REQUIRED with a non-empty,
        # coherently tiled chunks list; when R was disabled/failed the
        # outcome must be null. A malformed/missing R outcome is a FULL
        # cache miss, never a partial replay while audit issues stay
        # reusable.
        report_status = r_editor.get("status")
        if (
            not isinstance(report_status, str)
            or report_status not in _R_EDITOR_REPORT_STATUSES
        ):
            return f"r_editor.status is missing or unknown {report_status!r}"
        if not isinstance(r_editor.get("enabled"), bool):
            return "r_editor.enabled is not a bool"
        outcome = r_editor.get("outcome")
        if report_status in _R_EDITOR_RAN_STATUSES:
            # R ran: outcome REQUIRED with a non-empty chunks list.
            if not isinstance(outcome, dict):
                return (
                    f"r_editor.outcome is required when status is "
                    f"{report_status!r} (got {type(outcome).__name__})"
                )
            r_chunks = outcome.get("chunks")
            if not isinstance(r_chunks, list) or not r_chunks:
                return "r_editor.outcome.chunks is missing, not a list, or empty"
            chunk_size = r_editor.get("chunk_size")
            if (
                not isinstance(chunk_size, int)
                or isinstance(chunk_size, bool)
                or chunk_size < 1
            ):
                return f"r_editor.chunk_size {chunk_size!r} is not a positive int"
            if expected_pids is not None:
                expected_count = (
                    len(expected_pids) + chunk_size - 1
                ) // chunk_size
                if len(r_chunks) != expected_count:
                    return (
                        f"r_editor chunk coverage mismatch: {len(r_chunks)} "
                        f"chunk(s) persisted for {len(expected_pids)} pids at "
                        f"chunk_size {chunk_size} (expected {expected_count})"
                    )
            for position, item in enumerate(r_chunks, start=1):
                if not isinstance(item, dict):
                    return f"r_editor chunk at position {position}: not an object"
                if set(item) != _R_CHUNK_KEYS:
                    return (
                        f"r_editor chunk at position {position}: foreign key "
                        f"set {sorted(item)!r} (expected {sorted(_R_CHUNK_KEYS)})"
                    )
                chunk_index = item.get("chunk")
                if chunk_index != position:
                    return (
                        f"r_editor chunk coverage/order mismatch: stored index "
                        f"{chunk_index!r} at position {position} (expected "
                        f"contiguous 1..M)"
                    )
                status = item.get("status")
                if not isinstance(status, str) or status not in _R_EDITOR_CHUNK_STATUSES:
                    return f"r_editor chunk {chunk_index}: unknown status {status!r}"
                first_pid = item.get("first_pid")
                last_pid = item.get("last_pid")
                if not isinstance(first_pid, str) or not first_pid:
                    return f"r_editor chunk {chunk_index}: first_pid is missing or not a string"
                if not isinstance(last_pid, str) or not last_pid:
                    return f"r_editor chunk {chunk_index}: last_pid is missing or not a string"
                if expected_pids is not None:
                    # Coherent tiling: chunk k must cover exactly the k-th
                    # chunk_size span of the ordered translation map — chunk 1
                    # starts at map[0], chunk k+1 starts right after chunk k
                    # ends, the last chunk ends at map[-1]. A duplicate /
                    # missing / extra / foreign chunk record breaks the
                    # tiling and is a full miss.
                    start = (position - 1) * chunk_size
                    end = min(position * chunk_size, len(expected_pids))
                    if (
                        first_pid != expected_pids[start]
                        or last_pid != expected_pids[end - 1]
                    ):
                        return (
                            f"r_editor chunk {chunk_index}: boundary span "
                            f"{first_pid!r}/{last_pid!r} does not tile the "
                            f"translation map (expected "
                            f"{expected_pids[start]!r}/{expected_pids[end - 1]!r} "
                            f"at positions {start}..{end - 1})"
                        )
                edits = item.get("edits")
                if not isinstance(edits, list):
                    return f"r_editor chunk {chunk_index}: edits is not a list"
                chunk_pids = None
                if expected_pids is not None:
                    start = (position - 1) * chunk_size
                    end = min(position * chunk_size, len(expected_pids))
                    chunk_pids = frozenset(expected_pids[start:end])
                for edit_index, edit in enumerate(edits):
                    if not isinstance(edit, dict):
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"not an object"
                        )
                    if set(edit) != _R_EDIT_KEYS:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"foreign key set {sorted(edit)!r} "
                            f"(expected {sorted(_R_EDIT_KEYS)})"
                        )
                    edit_pid = edit.get("pid")
                    if not isinstance(edit_pid, str) or not edit_pid:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"pid is missing or not a string"
                        )
                    if expected_pid_set is not None and edit_pid not in expected_pid_set:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"pid {edit_pid!r} is not in the translation map"
                        )
                    if chunk_pids is not None and edit_pid not in chunk_pids:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"pid {edit_pid!r} is outside the chunk boundary "
                            f"span {first_pid!r}..{last_pid!r}"
                        )
                    original = edit.get("original")
                    if not isinstance(original, str) or not original:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: original is missing or not a string"
                        )
                    rewritten = edit.get("rewritten")
                    if not isinstance(rewritten, str) or not rewritten.strip():
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: rewritten is missing or not a string"
                        )
                    if not isinstance(edit.get("reason"), str) or not edit.get("reason").strip():
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: reason is missing or not a string"
                        )
                    klass = edit.get("class")
                    if not isinstance(klass, str) or klass not in ALL_CLASSES:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: unknown edit class {klass!r} "
                            f"(allowed: {sorted(ALL_CLASSES)})"
                        )
                    if current_text is not None and original not in str(
                        current_text.get(edit_pid, "")
                    ):
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: original is not a substring of the "
                            f"current text (tampered cache edit)"
                        )
        elif outcome is not None:
            # R disabled/failed: a non-null outcome is a schema violation.
            return (
                f"r_editor: outcome must be null when status is "
                f"{report_status!r} (got {type(outcome).__name__})"
            )

    # Canonical integrity binding: the whole partial replay payload must
    # match the hash persisted at save() time. A missing field (old
    # schema), any tamper, reorder, duplicate, or extra element — even
    # while identity and translations_repaired_hash are untouched — is a
    # full cache miss, never a partial replay.
    stored_hash = payload.get("partial_resume_hash")
    computed_hash = canonical_json_hash({
        "chunks": chunks, "issues": issues, "r_editor": r_editor,
    })
    if not isinstance(stored_hash, str) or stored_hash != computed_hash:
        return (
            "partial_resume_hash mismatch (old schema or tampered partial "
            "payload; stored=%r computed=%r)"
        ) % (stored_hash, computed_hash)
    return None


class B3AuditCache:
    """Persistent audit cache with resume identity (card §10 B3 item 3).

    Identity = snapshot_hash + translation_hash + config_identity +
    backend_identity_hash + prompt_version + harness_version +
    entity_context_hash (present only when entity context is enabled).
    ``audit_complete=True`` is a FULL hit (the stored repaired map is
    replayed, 0 model calls). ``audit_complete=False`` with an intact
    identity is a PARTIAL hit (PARTIAL-RESUME t_a58dd881): GOOD chunks are
    replayed per-chunk, failed chunks re-run — never a full replay of an
    incomplete audit (fail-closed), and never a replay across an identity
    mismatch (full miss, as before).
    """

    def __init__(self, path: Path, payload: Optional[Mapping[str, Any]] = None) -> None:
        self.path = Path(path)
        self._payload: Optional[Dict[str, Any]] = (
            dict(payload) if payload is not None else None
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        snapshot_hash: str,
        translation_hash: str,
        config_identity: str,
        backend_identity_hash: str,
        prompt_version: str,
        harness_version: str,
        entity_context_hash: Optional[str],
        entity_context_enabled: bool,
        expected_pids: Optional[Sequence[str]] = None,
        current_text: Optional[Mapping[str, str]] = None,
    ) -> Optional["B3AuditCache"]:
        if not Path(path).exists():
            return None
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("B3: audit cache unreadable (%s); re-running audit", exc)
            return None
        if not isinstance(payload, dict):
            LOG.warning("B3: audit cache is not an object; re-running audit")
            return None
        expected = {
            "snapshot_hash": snapshot_hash,
            "translation_hash": translation_hash,
            "config_identity": config_identity,
            "backend_identity_hash": backend_identity_hash,
            "prompt_version": prompt_version,
            "harness_version": harness_version,
            "entity_context_enabled": entity_context_enabled,
            "entity_context_hash": entity_context_hash,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                LOG.info(
                    "B3: audit cache identity mismatch on %s "
                    "(stored=%r expected=%r); re-running audit",
                    key, payload.get(key), value,
                )
                return None
        if payload.get("schema") != B3_AUDIT_CACHE_SCHEMA:
            LOG.info("B3: audit cache schema mismatch; re-running audit")
            return None
        # F4 (B3 review): the cached repaired map is validated before it can
        # ever be reused/publicized. A structurally tampered cache (extra /
        # missing / reordered PIDs, non-string values, or a repaired-map hash
        # that does not bind to the stored map) is a MISS — the audit re-runs
        # and the tampered map is never published. Old-schema caches (no
        # ``translations_repaired_hash`` field) also miss. The check also
        # guards the PARTIAL path: the edited-map fail-closed guard reads
        # ``stored_translations_repaired``, so a tampered map must be a miss
        # there too.
        repaired = payload.get("translations_repaired")
        if not isinstance(repaired, dict):
            LOG.warning(
                "B3: audit cache translations_repaired is not an object; "
                "re-running audit"
            )
            return None
        if expected_pids is not None:
            stored_pids = list(repaired.keys())
            if stored_pids != list(expected_pids):
                LOG.warning(
                    "B3: audit cache translations_repaired PID set mismatch "
                    "(stored=%r expected=%r); re-running audit",
                    stored_pids, list(expected_pids),
                )
                return None
        if any(not isinstance(value, str) for value in repaired.values()):
            LOG.warning(
                "B3: audit cache translations_repaired contains non-string "
                "values; re-running audit"
            )
            return None
        stored_hash = payload.get("translations_repaired_hash")
        computed_hash = canonical_json_hash(dict(sorted(repaired.items())))
        if not isinstance(stored_hash, str) or stored_hash != computed_hash:
            LOG.warning(
                "B3: audit cache translations_repaired hash mismatch "
                "(stored=%r computed=%r) — old schema or tampered cache; "
                "re-running audit",
                stored_hash, computed_hash,
            )
            return None
        if payload.get("audit_complete") is not True:
            # PARTIAL-RESUME (t_a58dd881): identity matched but the cached
            # audit did not complete. NOT a full miss anymore — the cache is
            # returned as a PARTIAL hit: GOOD chunks (status GOOD/GOOD_RETRIED,
            # valid JSON confirmed at write) and their top-level issues are
            # reused; TRANSPORT_ERROR/EMPTY/FAILED chunks are marked for
            # re-run. Fail-closed is preserved: the cached repaired map is
            # never replayed, the gate stays at audit_complete=False until
            # EVERY chunk has a status, and a cache is never downgraded.
            # PARTIAL-RESUME integrity (t_ec6bb8bc): BEFORE either resume
            # plan is constructed the complete replay payload (chunks /
            # issues / r_editor) is structurally validated and bound to its
            # persisted canonical hash — any malformed, missing, duplicate,
            # extra, or mismatched partial payload is a FULL cache miss
            # (the audit re-runs), never a partial replay. A tampered
            # payload can no longer publish unauthorized issues/edits with
            # 0 model calls while identity and translations_repaired_hash
            # stay intact.
            reason = _validate_partial_payload(
                payload,
                expected_pids=expected_pids,
                current_text=current_text,
            )
            if reason is not None:
                LOG.warning(
                    "B3: audit cache partial payload rejected (%s); "
                    "re-running the full audit (fail-closed)",
                    reason,
                )
                return None
            LOG.info(
                "B3: cached audit is incomplete (audit_complete=%r); "
                "PARTIAL resume — GOOD chunks reused, failed chunks re-run",
                payload.get("audit_complete"),
            )
            return cls(path, payload)
        return cls(path, payload)

    def is_hit(self) -> bool:
        return self._payload is not None

    def audit_complete(self) -> bool:
        return bool(self._payload and self._payload.get("audit_complete") is True)

    def entity_context_hash(self) -> Optional[str]:
        if not self._payload:
            return None
        return self._payload.get("entity_context_hash")

    def stored_issues(self) -> Tuple[Dict[str, Any], ...]:
        if not self._payload:
            return ()
        return tuple(self._payload.get("issues") or ())

    def stored_filtered(self) -> Tuple[FilteredIssue, ...]:
        if not self._payload:
            return ()
        filtered: list = []
        for item in self._payload.get("filtered") or ():
            filtered.append(FilteredIssue(**item))
        return tuple(filtered)

    def stored_repair(self) -> Optional[Dict[str, Any]]:
        if not self._payload:
            return None
        return self._payload.get("repair")

    def stored_translations_repaired(self) -> Optional[Dict[str, str]]:
        if not self._payload:
            return None
        value = self._payload.get("translations_repaired")
        # F4: values are validated at load() (non-string values reject the
        # cache), so no stringification is applied here — a tampered non-
        # string value must never be silently coerced into publication.
        if not isinstance(value, dict):
            return None
        return dict(value)

    def stored_r_editor(self) -> Optional[Dict[str, Any]]:
        """The stored V4.2 R-stage report (None when the cache predates R or
        the stage was disabled — the caller falls back to a disabled
        report)."""
        if not self._payload:
            return None
        value = self._payload.get("r_editor")
        if not isinstance(value, dict):
            return None
        return dict(value)

    # ------------------------------------------------------------------
    # PARTIAL-RESUME (t_a58dd881): per-chunk reuse plans
    # ------------------------------------------------------------------

    _GOOD_STATUSES = ("GOOD", "GOOD_RETRIED")

    def is_partial(self) -> bool:
        """True when the cached audit is a PARTIAL hit (identity matched,
        audit did not complete) — GOOD chunks reusable, failed re-run."""
        return bool(self._payload and self._payload.get("audit_complete") is not True)

    def audit_resume_plan(self) -> Dict[int, Dict[str, Any]]:
        """Per-chunk audit reuse plan for a partial cache.

        Returns ``{chunk_index: {status, first_pid, last_pid, pair_count,
        context_count, reasoning_chars, reasoning_file, finish_reason,
        issues}}`` for every cached chunk whose status is GOOD/GOOD_RETRIED
        (valid JSON was confirmed at write time — never re-validated here).
        ``issues`` are the top-level cached issues attributed to this chunk
        via their ``_debug.chunk`` (the deduped top-level list is the
        authoritative issue set; a GOOD_RETRIED chunk's sub-issues carry the
        parent chunk index). Empty when the cache is not a partial hit or
        has no reusable chunks.
        """
        if not self._payload:
            return {}
        chunk_payloads = self._payload.get("chunks")
        if not isinstance(chunk_payloads, list):
            return {}
        issues_by_chunk: Dict[int, List[Dict[str, Any]]] = {}
        for issue in self._payload.get("issues") or ():
            if not isinstance(issue, dict):
                continue
            debug = issue.get("_debug")
            chunk = debug.get("chunk") if isinstance(debug, dict) else None
            if isinstance(chunk, int):
                issues_by_chunk.setdefault(chunk, []).append(dict(issue))
        plan: Dict[int, Dict[str, Any]] = {}
        for chunk_payload in chunk_payloads:
            if not isinstance(chunk_payload, dict):
                continue
            if chunk_payload.get("status") not in self._GOOD_STATUSES:
                continue
            chunk_index = chunk_payload.get("chunk")
            if not isinstance(chunk_index, int):
                continue
            plan[chunk_index] = {
                "status": str(chunk_payload.get("status")),
                "first_pid": chunk_payload.get("first_pid"),
                "last_pid": chunk_payload.get("last_pid"),
                "pair_count": chunk_payload.get("pair_count"),
                "context_count": chunk_payload.get("context_count"),
                "reasoning_chars": chunk_payload.get("reasoning_chars", 0),
                "reasoning_file": chunk_payload.get("reasoning_file", ""),
                "finish_reason": chunk_payload.get("finish_reason"),
                "issues": issues_by_chunk.get(chunk_index, []),
            }
        return plan

    def r_editor_resume_plan(self) -> Dict[int, Dict[str, Any]]:
        """Per-chunk Russian-editor reuse plan for a partial cache.

        Returns ``{chunk_index: {status, first_pid, edits}}`` for every
        cached R chunk whose status is GOOD — ``edits`` are the parse-
        validated per-chunk edit payloads stored in the R report (replayed
        with 0 model calls on resume; the caller re-runs only the failed
        chunks). Empty when R was disabled / the cache predates R / the R
        stage had no GOOD chunks.
        """
        if not self._payload:
            return {}
        report = self._payload.get("r_editor")
        if not isinstance(report, dict):
            return {}
        outcome = report.get("outcome")
        if not isinstance(outcome, dict):
            return {}
        plan: Dict[int, Dict[str, Any]] = {}
        for chunk_payload in outcome.get("chunks") or ():
            if not isinstance(chunk_payload, dict):
                continue
            if chunk_payload.get("status") != "GOOD":
                continue
            chunk_index = chunk_payload.get("chunk")
            if not isinstance(chunk_index, int):
                continue
            # PARTIAL-RESUME integrity (t_ec6bb8bc): never coerce a
            # malformed edits field — a non-list is a payload violation and
            # the chunk is re-run (fail-closed per chunk), not stringified
            # into a replayable plan. The authoritative rejection happens in
            # B3AuditCache.load (full miss); this guard only keeps the plan
            # method safe when called on an unvalidated cache.
            edits = chunk_payload.get("edits")
            if not isinstance(edits, list):
                LOG.warning(
                    "B3: partial cache r_editor chunk %d edits is not a "
                    "list (%r) — chunk re-run (fail-closed)",
                    chunk_index, type(edits).__name__,
                )
                continue
            plan[chunk_index] = {
                "status": "GOOD",
                "first_pid": chunk_payload.get("first_pid"),
                "edits": list(edits),
            }
        return plan

    def save(
        self,
        *,
        snapshot_hash: str,
        translation_hash: str,
        config_identity: str,
        backend_identity_hash: str,
        entity_context_hash: Optional[str],
        entity_context_enabled: bool,
        outcome: ChunkedAuditOutcome,
        filtered: Sequence[FilteredIssue],
        repair: Optional[SelectiveRepairOutcome],
        translations_repaired: Mapping[str, str],
        r_editor: Optional[Mapping[str, Any]] = None,
    ) -> None:
        # PARTIAL-RESUME integrity (t_ec6bb8bc): the replay payload slices
        # are computed ONCE and both written and bound to a canonical hash.
        # load() recomputes the hash over the same slices before any resume
        # plan is built, so a tampered chunks/issues/r_editor payload — even
        # with identity and translations_repaired_hash preserved — is a full
        # cache miss, never a partial replay.
        chunks_payload = outcome.to_payload()["chunks"]
        issues_payload = [dict(issue) for issue in outcome.issues]
        r_editor_payload = dict(r_editor) if r_editor is not None else None
        payload = {
            "schema": B3_AUDIT_CACHE_SCHEMA,
            "snapshot_hash": snapshot_hash,
            "translation_hash": translation_hash,
            "config_identity": config_identity,
            "backend_identity_hash": backend_identity_hash,
            "prompt_version": outcome.prompt_version,
            "harness_version": outcome.harness_version,
            "entity_context_enabled": entity_context_enabled,
            "entity_context_hash": entity_context_hash,
            "audit_complete": outcome.audit_complete,
            "issue_count": outcome.issue_count,
            "issues": issues_payload,
            "chunks": chunks_payload,
            "filtered": [
                {
                    "issue": dict(f.issue),
                    "verdict": f.verdict,
                    "filter_name": f.filter_name,
                    "reason": f.reason,
                    # CANDIDATE-MERGE (t_0ffe56e1): the source stage is part
                    # of the cached filtered round-trip (old caches without
                    # the key restore the fidelity_auditor default).
                    "source_stage": f.source_stage,
                }
                for f in filtered
            ],
            "repair": repair.to_payload() if repair is not None else None,
            "translations_repaired": dict(translations_repaired),
            # V4.2 R: the Russian-editor report rides the audit cache so a
            # full cache hit restores the R outcome (edit_candidates +
            # accept/reject journal) with 0 model calls.
            "r_editor": r_editor_payload,
            # F4: canonical hash of the repaired map binds the map to this
            # cache record. load() recomputes it and rejects a mismatch (old
            # schema / tampered map), so a structurally tampered
            # translations_repaired can never be replayed or publicized.
            "translations_repaired_hash": canonical_json_hash(
                dict(sorted(translations_repaired.items()))
            ),
            # PARTIAL-RESUME integrity (t_ec6bb8bc): canonical hash of the
            # partial replay payload (chunks + issues + r_editor). load()
            # recomputes it on the partial branch and rejects a mismatch —
            # a tampered GOOD-chunk evidence/edit set is a full miss.
            "partial_resume_hash": canonical_json_hash({
                "chunks": chunks_payload,
                "issues": issues_payload,
                "r_editor": r_editor_payload,
            }),
        }
        _atomic_write_json(self.path, payload)
        self._payload = payload


# ---------------------------------------------------------------------------
# Orchestrator bundle
# ---------------------------------------------------------------------------


class B3AuditRepair:
    """Production B3 pipeline bundle (transport-neutral, injectable).

    Usage (CLI wires it from ``build_role_backend``)::

        b3 = B3AuditRepair(
            audit_backend=completion_backend,      # Qwen (qwen_audit role)
            repair_backend=completion_backend,     # generator role (Gemma)
            config=B3AuditRepairConfig(entity_context_enabled=cfg.entity_context_enabled),
        )
        result = b3.run(
            chapter_id=cfg.chapter_id,
            source=source,                          # SourceArtifact
            translation=dict(final_text_by_pid),    # raw generator map
            book_memory=memory.book_memory,
            out_dir=cfg.out_dir,
            config_identity=config.config_identity,
            backend_identity_hash=cfg.backend.identity_hash,
        )

    ``audit_backend`` must serve the ``qwen_audit`` (or ``default``)
    role; ``repair_backend`` the generator role (Kocmi-safe: auditor !=
    repairer). Entity extraction runs on the audit model.
    """

    def __init__(
        self,
        *,
        audit_backend: CompletionBackend,
        repair_backend: Optional[CompletionBackend] = None,
        entity_backend: Optional[CompletionBackend] = None,
        config: Optional[B3AuditRepairConfig] = None,
        progress: Optional[Any] = None,
    ) -> None:
        self._audit_backend = audit_backend
        self._repair_backend = repair_backend or audit_backend
        self._config = config or B3AuditRepairConfig()
        # Entity extraction runs on the audit (Qwen) model; the view adds
        # the missing entity_extractor role without touching backend
        # identity (role resolution only).
        from pact_v4.audit.chunked_audit import audit_model_ref

        entity_ref = audit_model_ref(audit_backend)
        self._entity_backend = entity_backend or _EntityRoleView(
            audit_backend, entity_ref
        )
        self._progress = progress

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        chapter_id: str,
        source: SourceArtifact,
        snapshot_hash: str,
        translation: Mapping[str, str],
        book_memory: Mapping[str, Any],
        out_dir: Path,
        config_identity: str,
        backend_identity_hash: str,
    ) -> B3AuditRepairResult:
        cfg = self._config
        cache_path = _audit_cache_path(out_dir)
        journal = AuditJournal(_journal_path(out_dir))
        try:
            return self._run_impl(
                chapter_id=chapter_id,
                source=source,
                snapshot_hash=snapshot_hash,
                translation=translation,
                book_memory=book_memory,
                out_dir=out_dir,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                cache_path=cache_path,
                journal=journal,
            )
        finally:
            journal.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _run_russian_editor(
        self,
        *,
        chapter_id: str,
        translation: Mapping[str, str],
        journal: AuditJournal,
        out_dir: Optional[Path],
        cached_chunks: Optional[Mapping[int, Mapping[str, Any]]] = None,
    ) -> Tuple[Dict[str, str], Tuple[ReviewCandidate, ...], Optional[RussianEditorOutcome]]:
        """V4.2 R stage: run the Russian-only editor over the raw map.

        Returns ``(edited_map, review_candidates, outcome)``:
        * edited_map = raw + SAFE edits (diff-gated) from SUCCESSFUL
          chunks, raw when the stage is disabled or failed;
        * review_candidates = REVIEW-classed edits (never auto-applied)
          from SUCCESSFUL chunks, empty when disabled/failed;
        * outcome = the evaluator outcome (None when disabled).

        Partial-apply (RESILIENCE t_406fc48c, run_remote_001): R edits are
        per-chunk independent, so a failed chunk discards ONLY its own
        edits — the successful chunks' edits are applied and their
        candidates forwarded. Fail-closed is preserved PER-CHUNK: the
        evaluator routes no edits for a failed chunk (``outcome.edits`` /
        ``applied`` / ``candidates`` already contain only GOOD-chunk
        content), and the journal records ``r_editor_done`` with
        ``partial=true`` + ``applied_count`` + ``failed_chunks``. The
        failure is debt (journal + outcome), exactly like a failed repair
        batch: the audit still protects the chapter.

        PARTIAL-RESUME (t_a58dd881): ``cached_chunks`` (from the audit
        cache's r_editor report) replays GOOD chunks with 0 model calls —
        their parse-validated edits are re-applied to the raw map
        (idempotent: R applies edits to the RAW map at run time, so a
        replayed GOOD chunk reproduces the exact same edited text); the
        failed chunks are re-run.
        """
        cfg = self._config
        if not cfg.russian_editor_enabled:
            return dict(translation), (), None

        def _journal_r_editor_chunk_event(kind: str, fields: Dict[str, Any]) -> None:
            # A2 (run_011): per-chunk causality in the append-only journal —
            # started BEFORE the model call, terminal done AFTER it, with the
            # FAILED reason (parse/transport) so a failed R chunk is
            # diagnosable even without reading the raw artifact.
            if kind == "started":
                journal.emit(
                    "r_editor_chunk_started",
                    chunk=fields.get("chunk"),
                    total=fields.get("total"),
                )
            elif kind == "retry":
                # R-RETRY (t_8ab8ab35): a bounded retry attempt (transport
                # or invalid JSON/empty body) is journaled so retry
                # causality is visible (acceptance: retry attempts in the
                # journal).
                journal.emit(
                    "r_editor_chunk_retry",
                    chunk=fields.get("chunk"),
                    attempt=fields.get("attempt"),
                    total=fields.get("total"),
                    error=fields.get("error"),
                    delay=fields.get("delay"),
                )
            else:
                journal.emit(
                    "r_editor_chunk_done",
                    chunk=fields.get("chunk"),
                    total=fields.get("total"),
                    status=fields.get("status"),
                    edit_count=fields.get("edit_count", 0),
                    warning_count=fields.get("warning_count", 0),
                    error=fields.get("error"),
                    # PARTIAL-RESUME: the chunk was replayed from the partial
                    # cache (0 model calls), not freshly edited.
                    reused=fields.get("reused"),
                )

        evaluator = RussianEditorEvaluator(
            self._audit_backend,
            config=RussianEditorConfig(
                chunk_size=cfg.russian_editor_chunk_size,
                overlap_pairs=cfg.russian_editor_overlap_pairs,
                max_tokens=cfg.russian_editor_max_tokens,
                safe_classes=cfg.russian_editor_safe_classes,
                harness_version=cfg.russian_editor_harness_version,
                prompt_version=cfg.russian_editor_version,
                max_edits_per_pid=cfg.russian_editor_max_edits_per_pid,
                retry_max_retries=cfg.russian_editor_retry_max_retries,
                retry_base_delay_seconds=cfg.russian_editor_retry_base_delay_seconds,
            ),
            on_chunk_event=_journal_r_editor_chunk_event,
        )
        journal.emit(
            "r_editor_started",
            enabled=True,
            chunk_size=cfg.russian_editor_chunk_size,
            overlap_pairs=cfg.russian_editor_overlap_pairs,
            safe_classes=sorted(cfg.russian_editor_safe_classes),
            prompt_version=cfg.russian_editor_version,
        )
        try:
            outcome = evaluator(
                chapter_id=chapter_id, translation=translation,
                out_dir=out_dir, out_base="r_editor",
                cached_chunks=cached_chunks,
            )
        except Exception as exc:  # noqa: BLE001 — R failure is debt, never a crash
            LOG.exception("B3: russian_editor failed for %s", chapter_id)
            journal.emit(
                "r_editor_done",
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return dict(translation), (), None
        if not outcome.complete:
            # RESILIENCE (t_406fc48c, run_remote_001): partial-apply — R
            # edits are per-chunk independent; a failed chunk discards ONLY
            # its own edits, never the successful chunks' work. The
            # evaluator's outcome.applied/edits/candidates already contain
            # only GOOD-chunk content (fail-closed per chunk), so applying
            # them is safe. run_remote_001: 17 edits from 5 GOOD chunks
            # were dropped because 3 chunks failed (incl. the p00070 typo
            # 'Не важно'→'Неважно').
            edited_map = {**dict(translation), **dict(outcome.applied)}
            LOG.warning(
                "B3: russian_editor partial for %s (failed chunks %s) — "
                "applied %d edit(s) from %d successful chunk(s), failed "
                "chunk edits skipped (per-chunk fail-closed); audit "
                "proceeds on the partially edited map",
                chapter_id, list(outcome.failed_chunks),
                len(outcome.applied), outcome.successful_chunks,
            )
            journal.emit(
                "r_editor_done",
                status="partial",
                partial=True,
                failed_chunks=list(outcome.failed_chunks),
                successful_chunks=outcome.successful_chunks,
                chunk_count=outcome.chunk_count,
                edit_count=len(outcome.edits),
                applied_count=len(outcome.applied),
                candidate_count=len(outcome.candidates),
                dropped=outcome.dropped,
                warning_count=outcome.warning_count,
            )
            self._emit_progress(
                "r_editor_done",
                complete=False,
                partial=True,
                applied_count=len(outcome.applied),
                candidate_count=len(outcome.candidates),
            )
            return edited_map, outcome.candidates, outcome
        edited_map = {**dict(translation), **dict(outcome.applied)}
        journal.emit(
            "r_editor_done",
            status="complete",
            chunk_count=outcome.chunk_count,
            successful_chunks=outcome.successful_chunks,
            edit_count=len(outcome.edits),
            applied_count=len(outcome.applied),
            candidate_count=len(outcome.candidates),
            dropped=outcome.dropped,
            warning_count=outcome.warning_count,
        )
        self._emit_progress(
            "r_editor_done",
            complete=True,
            applied_count=len(outcome.applied),
            candidate_count=len(outcome.candidates),
        )
        return edited_map, outcome.candidates, outcome

    def _run_impl(
        self,
        *,
        chapter_id: str,
        source: SourceArtifact,
        snapshot_hash: str,
        translation: Mapping[str, str],
        book_memory: Mapping[str, Any],
        out_dir: Path,
        config_identity: str,
        backend_identity_hash: str,
        cache_path: Path,
        journal: AuditJournal,
    ) -> B3AuditRepairResult:
        cfg = self._config
        source_map = dict(source.source)
        translation_map = dict(translation)
        # The audit outcome is a function of BOTH the source and the
        # translation being audited, so the audit cache identity binds to
        # the exact translation content too — a regenerated/tampered raw
        # map with the same snapshot hash must never be a stale cache hit.
        # V4.2 R: the identity binds the RAW map hash plus the R config
        # keys (config_identity carries russian_editor_version + chunk
        # settings + class threshold), so a cache produced under a different
        # editor policy never replays (F5 lesson) and R itself runs ONLY on
        # a cache miss — a full hit restores the stored R report and the
        # repaired map with 0 model calls (the resume contract).
        translation_hash = canonical_json_hash(dict(sorted(translation_map.items())))

        # ------------------------------------------------------------------
        # 1. Entity context prepass (B1.2), when enabled.
        # ------------------------------------------------------------------
        entity_context: str = ""
        entity_hash: Optional[str] = None
        entity_payload: Optional[Mapping[str, Any]] = None
        entity_from_cache = False
        if cfg.entity_context_enabled:
            entity_cache = _load_entity_cache(out_dir)
            try:
                extraction = extract_entity_context(
                    source_artifact=source,
                    extractor=BackendEntityExtractor(
                        self._entity_backend,
                        config=BackendEntityExtractorConfig(),
                    ),
                    cache=entity_cache,
                    extractor_version=cfg.extractor_version,
                    out_dir=out_dir,
                )
            except Exception as exc:  # noqa: BLE001 — fail-closed, never silent skip
                LOG.exception("B3: entity context extraction failed for %s", chapter_id)
                raise RuntimeError(
                    f"B3 entity context extraction failed: {exc}"
                ) from exc
            entity_from_cache = extraction.from_cache
            entity_payload = extraction.context.to_payload()
            entity_hash = canonical_json_hash(entity_payload)
            entity_context = render_entity_context_block(extraction.context)
            _save_entity_cache(out_dir, entity_cache)
            # B3-DIAG transparency: what the model proposed vs what the code
            # accepted. A fresh extraction's validation report is persisted
            # next to the cache; a cache hit reuses the previously validated
            # context (validation report empty), so the original report is
            # kept, never overwritten with an empty one.
            if not extraction.from_cache:
                _save_entity_validation_report(
                    out_dir, extraction.validation.to_payload()
                )
            journal.emit(
                "entity_context",
                enabled=True,
                from_cache=entity_from_cache,
                entity_count=len(extraction.context.entities),
                entity_context_hash=entity_hash,
            )
            self._emit_progress(
                "entity_context_done",
                from_cache=entity_from_cache,
                entity_count=len(extraction.context.entities),
            )

        # ------------------------------------------------------------------
        # 2. Audit cache: FULL hit -> reuse (0 model calls); PARTIAL hit
        #    (identity matched, audit incomplete) -> GOOD chunks reused,
        #    failed chunks re-run (PARTIAL-RESUME t_a58dd881).
        # ------------------------------------------------------------------
        cache = B3AuditCache.load(
            cache_path,
            snapshot_hash=snapshot_hash,
            translation_hash=translation_hash,
            config_identity=config_identity,
            backend_identity_hash=backend_identity_hash,
            prompt_version=cfg.prompt_version,
            harness_version=cfg.harness_version,
            entity_context_hash=entity_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            # F4: exact PID set/order validation — a cache whose
            # translations_repaired has missing/extra/reordered PIDs is a miss.
            expected_pids=tuple(translation_map),
            # PARTIAL-RESUME integrity (t_ec6bb8bc): the current (raw)
            # translation text lets the partial-payload validator enforce the
            # verbatim current-text substring constraint on every cached R
            # edit before any resume plan is built.
            current_text=translation_map,
        )
        # PARTIAL-RESUME: build the per-chunk reuse plans from a partial
        # cache (identity matched, audit_complete=False). GOOD audit chunks
        # and GOOD R chunks are replayed with 0 model calls; the failed ones
        # are re-run. The plans are consumed by the R stage and the audit
        # evaluator below.
        partial_cache = (
            cache if (cache is not None and cache.is_hit() and not cache.audit_complete())
            else None
        )
        audit_resume: Dict[int, Dict[str, Any]] = {}
        r_editor_resume: Dict[int, Dict[str, Any]] = {}
        # PARTIAL-RESUME: PIDs whose edited text changed in the R re-run vs
        # the cached edited map — cached audit chunks over those PIDs are
        # re-audited (per-chunk fail-closed). None when no partial reuse.
        resume_changed_pids: Optional[AbstractSet[str]] = None
        if partial_cache is not None:
            audit_resume = partial_cache.audit_resume_plan()
            r_editor_resume = partial_cache.r_editor_resume_plan()
            LOG.info(
                "B3: partial audit cache for %s — reusing %d GOOD audit "
                "chunk(s) + %d GOOD R chunk(s), re-running the failed ones",
                chapter_id, len(audit_resume), len(r_editor_resume),
            )
        if cache is not None and cache.is_hit() and cache.audit_complete():
            repaired = cache.stored_translations_repaired()
            issues = cache.stored_issues()
            filtered = cache.stored_filtered()
            repair_payload = cache.stored_repair()
            step6, step7, step8 = _reports_from_cache(
                cache=cache, issue_count=len(issues),
            )
            cache_repair_complete = bool(
                repair_payload and repair_payload.get("repair_complete") is True
            )
            LOG.info(
                "B3: audit cache full hit for %s (0 model calls)", chapter_id
            )
            journal.emit(
                "audit_started",
                snapshot_hash=snapshot_hash,
                entity_context_enabled=cfg.entity_context_enabled,
                entity_context_hash=entity_hash,
                prompt_version=cfg.prompt_version,
                harness_version=cfg.harness_version,
            )
            journal.emit(
                "audit_complete",
                audit_complete=True,
                issue_count=len(issues),
                from_cache=True,
            )
            journal.emit(
                "gate",
                audit_complete=True,
                # F1: a cached repair with repair_complete=False (failed
                # batch / failed re-audit) is replayed as NOT released — the
                # cache hit must never upgrade debt into an audited release.
                released_as_audited=cache_repair_complete,
                repair_complete=cache_repair_complete,
                from_cache=True,
            )
            return B3AuditRepairResult(
                step6=step6,
                step7=step7,
                step8=step8,
                translations_repaired=(
                    repaired if repaired is not None else dict(translation_map)
                ),
                audit_complete=True,
                from_cache=True,
                entity_context_hash=cache.entity_context_hash(),
                audit_cache_path=cache_path,
                journal_path=journal.path,
                # V4.2 R: restore the Russian-editor report from the cache so
                # the trial record shows the original R outcome (candidates +
                # accept/reject journal) even on a 0-call replay.
                r_editor=cache.stored_r_editor(),
            )

        journal.emit(
            "audit_started",
            snapshot_hash=snapshot_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            entity_context_hash=entity_hash,
            prompt_version=cfg.prompt_version,
            harness_version=cfg.harness_version,
            # PARTIAL-RESUME (t_a58dd881): the audit started from a partial
            # cache — GOOD chunks replayed, failed chunks re-run.
            partial_resume=bool(audit_resume or r_editor_resume),
            reused_audit_chunks=sorted(audit_resume),
            reused_r_editor_chunks=sorted(r_editor_resume),
        )

        # ------------------------------------------------------------------
        # 2.5 V4.2 R: Russian-only editor stage (card t_4707e6e5). Runs on a
        #     cache MISS only (a full hit restores the stored R report with
        #     0 model calls). The R config participates in the config
        #     identity, so a policy change invalidates the repaired cache
        #     (F5 lesson) and the audit below runs on the R-EDITED map.
        # ------------------------------------------------------------------
        r_editor_outcome: Optional[RussianEditorOutcome] = None
        review_candidates: Tuple[ReviewCandidate, ...] = ()
        r_editor_report: Optional[Dict[str, Any]] = None
        if cfg.russian_editor_enabled:
            edited_map, review_candidates, r_editor_outcome = (
                self._run_russian_editor(
                    chapter_id=chapter_id,
                    translation=translation_map,
                    journal=journal,
                    out_dir=out_dir,
                    cached_chunks=r_editor_resume or None,
                )
            )
            # The audit/repair consume the R-EDITED map (raw + SAFE edits).
            translation_map = edited_map
            r_editor_report = _build_r_editor_report(
                cfg=cfg,
                outcome=r_editor_outcome,
                review_journal=(),
                from_cache=False,
            )
            # PARTIAL-RESUME fail-closed guard: the cached audit chunks were
            # computed on the R-EDITED map of the ORIGINAL run (the partial
            # cache's translations_repaired IS that edited map). If the R
            # re-run of previously-failed chunks changed the edited text, the
            # cached audit chunks that CONTAIN those PIDs are stale — the
            # evaluator refuses to replay a cached chunk whose own pids
            # intersect ``changed_pids`` (re-audits it) while chunks over
            # untouched PIDs are still replayed (per-chunk fail-closed, the
            # owner's "не пересматриваем GOOD" with input-identity honesty).
            # A missing cached edited map (legacy/tampered cache) drops the
            # whole reuse plan.
            if audit_resume:
                cached_edited = partial_cache.stored_translations_repaired()
                new_edited = dict(translation_map)
                if cached_edited is None:
                    LOG.warning(
                        "B3: partial audit cache for %s — no cached edited "
                        "map to verify audit input; full re-audit (fail-closed)",
                        chapter_id,
                    )
                    audit_resume = {}
                else:
                    resume_changed_pids = {
                        pid for pid, text in new_edited.items()
                        if cached_edited.get(pid) != text
                    }
                    if resume_changed_pids:
                        LOG.warning(
                            "B3: partial audit cache for %s — R re-run "
                            "changed %d PID(s); cached audit chunks over "
                            "those PIDs will be re-audited (per-chunk "
                            "fail-closed)",
                            chapter_id, len(resume_changed_pids),
                        )
            # Artifacts are written when the R stage produced ANY edited map
            # (complete pass, or a partial pass whose successful chunks
            # applied edits / forwarded candidates) — the audit/repair
            # consume that exact map, so translations_edited.json must
            # reflect it (F8: never advertise provenance the stage did not
            # produce; a fully-failed pass — 0 successful chunks — leaves
            # no artifacts and the audit runs on the raw map).
            if r_editor_outcome is not None and (
                r_editor_outcome.complete
                or r_editor_outcome.applied
                or r_editor_outcome.candidates
            ):
                _write_r_editor_artifacts(
                    cfg=cfg,
                    chapter_id=chapter_id,
                    snapshot_hash=snapshot_hash,
                    config_identity=config_identity,
                    out_dir=out_dir,
                    edited_map=translation_map,
                    candidates=review_candidates,
                )

        # ------------------------------------------------------------------
        # 3. Chunked audit (B1).
        # ------------------------------------------------------------------
        # F2 (RV2): the audit stage is wrapped so a pre/model-call evaluator
        # failure (CoverageError/empty input, BudgetOverflowError, missing
        # role, …) writes a TERMINAL audit failure event and a fail-closed
        # gate into the append-only journal BEFORE the exception propagates
        # to the strict runner — the journal must never end on
        # audit_started/started alone. Per-chunk TRANSPORT_ERROR failures
        # are handled inside the evaluator (failed chunks -> audit_complete
        # False -> the fail-closed gate below); that transport path is
        # preserved unchanged.
        try:
            pairs = pairs_from_maps(source_map, translation_map)
            narrator_context = build_narrator_context(
                book_memory, " ".join(source_map.values())
            )
            def _journal_chunk_event(kind: str, fields: Dict[str, Any]) -> None:
                # F7: journal causality — the started event is emitted BEFORE the
                # model call (inside ChunkedAuditEvaluator._run_one_chunk) and the
                # terminal done/failed after it, so a crash during a chunk leaves
                # item-start evidence in the append-only journal instead of nothing.
                if kind == "started":
                    journal.emit(
                        "audit_chunk_started",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        sub=fields.get("sub") or "",
                    )
                elif kind == "retry":
                    # R-RETRY (t_8ab8ab35): a TRANSPORT_ERROR chunk is retried
                    # with a NEW session — the retry attempt is journaled so
                    # the operator sees the transport blip and its backoff.
                    journal.emit(
                        "audit_chunk_retry",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        attempt=fields.get("attempt"),
                        error=fields.get("error"),
                        delay=fields.get("delay"),
                    )
                else:
                    journal.emit(
                        "audit_chunk_done",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        status=fields.get("status"),
                        issue_count=fields.get("issue_count", 0),
                        error=fields.get("error"),
                        # PARTIAL-RESUME: the chunk was replayed from the
                        # partial cache (0 model calls), not freshly audited.
                        reused=fields.get("reused"),
                    )

            evaluator = ChunkedAuditEvaluator(
                self._audit_backend,
                config=ChunkedAuditConfig(
                    max_input_tokens=cfg.max_input_tokens,
                    max_tokens=cfg.max_tokens,
                    overlap_tokens=cfg.overlap_tokens,
                    reasoning_budget=cfg.reasoning_budget,
                    harness_version=cfg.harness_version,
                    prompt_version=cfg.prompt_version,
                    # R-RETRY (t_8ab8ab35, F5): the chunk-level TRANSPORT_ERROR
                    # retry policy is wired from the B3 config (identity
                    # carries it) — never silently left at module defaults.
                    transport_max_retries=cfg.audit_transport_max_retries,
                    transport_base_delay_seconds=cfg.audit_transport_base_delay_seconds,
                ),
                on_chunk_event=_journal_chunk_event,
            )
            outcome = evaluator(
                chapter_id=chapter_id,
                pairs=pairs,
                narrator_context=narrator_context,
                entity_context=entity_context,
                out_dir=out_dir,
                out_base="b3_audit",
                # PARTIAL-RESUME: replay GOOD cached chunks (0 model calls);
                # chunks whose pids changed in the R re-run are re-audited.
                cached_chunks=audit_resume or None,
                resume_changed_pids=resume_changed_pids,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed: a pre/model-call
            # audit failure is TERMINAL. The strict runner records the failed
            # step fields, but the B3 journal must carry its own terminal
            # failure event + fail-closed gate/provenance too — recorded
            # BEFORE the exception is re-raised.
            LOG.exception("B3: chunked audit evaluator failed for %s", chapter_id)
            error = f"{type(exc).__name__}: {exc}"
            journal.emit(
                "audit_failed",
                error=error,
                audit_complete=False,
            )
            journal.emit(
                "gate",
                audit_complete=False,
                released_as_audited=False,
                error=error,
            )
            raise
        journal.emit(
            "audit_complete",
            audit_complete=outcome.audit_complete,
            issue_count=outcome.issue_count,
            failed_chunks=list(outcome.failed_chunks),
            from_cache=False,
            # PARTIAL-RESUME (t_a58dd881): how many GOOD chunks were replayed
            # from the partial cache (0 model calls for them).
            reused_chunks=sorted(audit_resume) if audit_resume else [],
        )
        self._emit_progress(
            "b3_audit_done",
            audit_complete=outcome.audit_complete,
            issue_count=outcome.issue_count,
            chunk_count=outcome.chunk_count,
            successful_chunks=outcome.successful_chunks,
            failed_chunks=list(outcome.failed_chunks),
        )

        # ------------------------------------------------------------------
        # 4. Gate: incomplete audit -> fail-closed (never repair, never
        #    release the chapter as passed audit).
        # ------------------------------------------------------------------
        if not outcome.audit_complete:
            LOG.error(
                "B3: audit incomplete for %s (failed chunks %s) — "
                "fail-closed: chapter is NOT released as passed audit",
                chapter_id, list(outcome.failed_chunks),
            )
            cache_writer = B3AuditCache(cache_path)
            cache_writer.save(
                snapshot_hash=snapshot_hash,
                translation_hash=translation_hash,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                entity_context_hash=entity_hash,
                entity_context_enabled=cfg.entity_context_enabled,
                outcome=outcome,
                filtered=(),
                repair=None,
                translations_repaired=dict(translation_map),
                # PARTIAL-RESUME (t_a58dd881): the R report (with per-chunk
                # statuses + parse-validated edits) rides the partial cache so
                # the NEXT resume reuses GOOD R chunks too (previously it was
                # dropped on the fail-closed path — r_editor stayed None and
                # run_remote_007-style caches lost all R reuse data).
                r_editor=r_editor_report,
            )
            step6 = {
                "status": "incomplete",
                "audit_complete": False,
                "chunk_count": outcome.chunk_count,
                "successful_chunks": outcome.successful_chunks,
                "failed_chunks": list(outcome.failed_chunks),
                "issue_count": outcome.issue_count,
                "entity_context_enabled": cfg.entity_context_enabled,
                "entity_context_hash": entity_hash,
                "from_cache": False,
                # PARTIAL-RESUME (t_a58dd881): GOOD chunks were replayed from
                # the partial cache (0 model calls); only the failed ones were
                # re-run (and failed again — fail-closed preserved).
                "partial_resume": bool(audit_resume),
                "reused_chunks": sorted(audit_resume) if audit_resume else [],
            }
            step7 = {"status": "skipped", "reason": "audit_incomplete_fail_closed"}
            step8 = {
                "status": "fail_closed_audit_incomplete",
                "audit_complete": False,
                "released_as_audited": False,
            }
            journal.emit(
                "gate",
                audit_complete=False,
                released_as_audited=False,
                failed_chunks=list(outcome.failed_chunks),
            )
            return B3AuditRepairResult(
                step6=step6,
                step7=step7,
                step8=step8,
                translations_repaired=dict(translation_map),
                audit_complete=False,
                from_cache=False,
                entity_context_hash=entity_hash,
                audit_cache_path=cache_path,
                journal_path=journal.path,
                # The R stage already ran (edits recorded) but the audit is
                # fail-closed — the R report is still surfaced for the trial
                # record (the chapter is not released as audited).
                r_editor=r_editor_report,
            )

        # ------------------------------------------------------------------
        # 5. Hard filters (B1.1) — entity-PID issues are forced TIER_B.
        # ------------------------------------------------------------------
        filtered = apply_hard_filters(
            outcome.issues,
            source=source_map,
            translation=translation_map,
            entity_context=entity_payload,
        )
        for f in filtered:
            journal.emit(
                "finding",
                pid=str(f.issue.get("id", "")),
                category=str(f.issue.get("category", "")),
                severity=str(f.issue.get("severity", "")),
                confidence=str(f.issue.get("confidence", "")),
                verdict=f.verdict,
                filter_name=f.filter_name,
                reason=f.reason,
            )

        # ------------------------------------------------------------------
        # 6. Selective repair (B2) + single re-audit of changed PIDs.
        # ------------------------------------------------------------------
        repair_outcome: Optional[SelectiveRepairOutcome] = None
        try:
            repair_evaluator = SelectiveRepairEvaluator(
                self._repair_backend,
                reaudit_backend=self._audit_backend,
                config=SelectiveRepairConfig(
                    findings_cap=cfg.repair_findings_cap,
                    microbatch_trigger=cfg.repair_microbatch_trigger,
                    microbatch_target=cfg.repair_microbatch_target,
                    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding, F5): the
                    # REPAIR prompt/harness version is wired from the B3
                    # config (the run identity carries it) — the evaluator
                    # can never silently fall back to a stale module default,
                    # and a cache written under a different repair prompt
                    # must never replay the repaired map.
                    prompt_version=cfg.repair_prompt_version,
                    harness_version=cfg.repair_harness_version,
                    # REPAIR-CTX (t_97b31f81, F5): the local-context window is
                    # wired from the B3 config (identity carries it), so the
                    # evaluator can never silently fall back to module
                    # defaults — a window change invalidates the repaired map
                    # cache.
                    repair_context_window=cfg.repair_context_window,
                    # REPAIR-2 (t_768537b9, F5): the per-category window
                    # overrides are wired from the B3 config (identity
                    # carries them), so the evaluator can never silently fall
                    # back to module defaults — a per-category window change
                    # invalidates the repaired map cache.
                    repair_context_window_by_category=dict(
                        cfg.repair_context_window_by_category
                    ),
                    reaudit_neighbour_window=cfg.repair_reaudit_neighbour_window,
                    # REPAIR-CTX (t_97b31f81, F5): the re-audit chunk/overlap
                    # settings and the REPAIRED CHANGES delta format are
                    # wired from the B3 config (identity carries them), so the
                    # evaluator can never silently fall back to module
                    # defaults — a chunk/delta change invalidates the repaired
                    # map cache.
                    reaudit_max_input_tokens=cfg.repair_reaudit_max_input_tokens,
                    reaudit_overlap_tokens=cfg.repair_reaudit_overlap_tokens,
                    reaudit_min_overlap_pairs=cfg.repair_reaudit_min_overlap_pairs,
                    reaudit_max_overlap_pairs=cfg.repair_reaudit_max_overlap_pairs,
                    reaudit_delta_format=cfg.repair_reaudit_delta_format,
                    # RV 71b7cbc fix (F5): the re-audit output budget and the
                    # bounded B4 JSON retry policy are wired from the B3
                    # config — the identity carries them, so the evaluator
                    # can never silently fall back to module defaults.
                    reaudit_max_tokens=cfg.repair_reaudit_max_tokens,
                    reaudit_retry=JsonRetryPolicy(
                        max_retries=cfg.repair_reaudit_max_retries,
                        base_delay_seconds=cfg.repair_reaudit_base_delay_seconds,
                    ),
                ),
            )
            repair_outcome = repair_evaluator(
                chapter_id=chapter_id,
                source=source_map,
                translation=translation_map,
                filtered=filtered,
                entity_context=entity_context,
                narrator_context=narrator_context,
                # V4.2 R: REVIEW-classed Russian-editor candidates are
                # additional verify-before-repair input — the verifier
                # accepts/rejects each against the ORIGINAL; accepted ones
                # are committed and covered by the re-audit.
                review_candidates=review_candidates,
                # B1/C1 (run_011): persist repair-batch + re-audit raw and
                # reasoning artifacts next to the audit cache.
                out_dir=out_dir,
                out_base="b3_repair",
            )
        except Exception as exc:  # noqa: BLE001 — a repair failure is debt, never a crash
            LOG.exception("B3: selective repair failed for %s", chapter_id)
            repair_outcome = None
            # Fall through: the cache is written with repair=None and the
            # gate stays honest (repair_complete=False).

        if repair_outcome is not None:
            journal.emit(
                "repair_round",
                round=1,
                eligible_count=repair_outcome.eligible_count,
                committed_pids=[pid for pid, _ in repair_outcome.committed],
                passed_pids=list(repair_outcome.passed_pids),
                debt_trace=list(repair_outcome.debt_trace),
                # REPAIR-2 (t_768537b9): per-index non-fatal notices (no-op
                # repairs converted to per-index pass) — journaled with the
                # round so the operator sees the model misused the decision
                # contract without losing the batch's real repairs.
                warnings=list(repair_outcome.warnings),
                repair_complete=repair_outcome.repair_complete,
                skipped=repair_outcome.skipped,
            )
            reaudit = repair_outcome.reaudit
            if reaudit is not None:
                journal.emit(
                    "reaudit_scope",
                    scope_pids=list(reaudit.scope),
                    full=reaudit.full,
                    issue_count=len(reaudit.issues),
                    failed=reaudit.failed,
                )

        committed = (
            {pid: text for pid, text in repair_outcome.committed}
            if repair_outcome is not None
            else {}
        )
        translations_repaired = {**translation_map, **committed}
        repair_complete = (
            repair_outcome.repair_complete if repair_outcome is not None else False
        )
        # V4.2 R: fold the accept/reject journal into the R report so the
        # trial record and the cached report carry it (on a full cache hit
        # the journal is restored verbatim from the cache payload).
        if r_editor_report is not None:
            review_journal = (
                repair_outcome.review_journal if repair_outcome is not None else ()
            )
            r_editor_report["review_journal"] = [
                dict(entry) for entry in review_journal
            ]

        # ------------------------------------------------------------------
        # 7. Persist cache + build reports.
        # ------------------------------------------------------------------
        cache_writer = B3AuditCache(cache_path)
        cache_writer.save(
            snapshot_hash=snapshot_hash,
            translation_hash=translation_hash,
            config_identity=config_identity,
            backend_identity_hash=backend_identity_hash,
            entity_context_hash=entity_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            outcome=outcome,
            filtered=filtered,
            repair=repair_outcome,
            translations_repaired=translations_repaired,
            r_editor=r_editor_report,
        )

        step6 = {
            "status": "complete",
            "audit_complete": True,
            "chunk_count": outcome.chunk_count,
            "successful_chunks": outcome.successful_chunks,
            "failed_chunks": list(outcome.failed_chunks),
            "issue_count": outcome.issue_count,
            "entity_context_enabled": cfg.entity_context_enabled,
            "entity_context_hash": entity_hash,
            "from_cache": False,
            # PARTIAL-RESUME (t_a58dd881): GOOD chunks were replayed from the
            # partial cache (0 model calls); only the failed ones re-run.
            "partial_resume": bool(audit_resume),
            "reused_chunks": sorted(audit_resume) if audit_resume else [],
        }
        step7 = {
            "status": (
                "complete" if repair_complete else (
                    "failed" if repair_outcome is None else "incomplete"
                )
            ),
            "repair_complete": repair_complete,
            "eligible_count": (
                repair_outcome.eligible_count if repair_outcome is not None else 0
            ),
            "committed_pids": [pid for pid, _ in committed.items()],
            "passed_pids": (
                list(repair_outcome.passed_pids) if repair_outcome is not None else []
            ),
            "debt_trace": (
                list(repair_outcome.debt_trace) if repair_outcome is not None else []
            ),
            # REPAIR-2 (t_768537b9): per-index non-fatal notices (no-op
            # repairs converted to per-index pass) ride the repair record so
            # the operator can see them in step7 / the trial record.
            "warnings": (
                list(repair_outcome.warnings) if repair_outcome is not None else []
            ),
        }
        # F1 (B3 review): the terminal gate is honest about repair debt. The
        # chapter is released as audited ONLY when the audit completed AND
        # the repair completed (every batch GOOD and the post-repair re-audit
        # succeeded). repair_complete=False (failed batch / failed re-audit /
        # repair exception) degrades the release to accepted_degraded with
        # released_as_audited=False — never a silent complete/PASS.
        if repair_complete:
            step8 = {
                "status": "complete",
                "audit_complete": True,
                "released_as_audited": True,
            }
        else:
            step8 = {
                "status": "accepted_degraded",
                "audit_complete": True,
                "released_as_audited": False,
                "repair_complete": False,
                "reason": (
                    "repair_failed" if repair_outcome is None else "repair_incomplete"
                ),
                "debt_trace": step7["debt_trace"],
            }
        journal.emit(
            "gate",
            audit_complete=True,
            released_as_audited=repair_complete,
            repair_complete=repair_complete,
        )
        self._emit_progress(
            "b3_repair_done",
            repair_complete=repair_complete,
            committed_pids=[pid for pid, _ in committed.items()],
        )

        return B3AuditRepairResult(
            step6=step6,
            step7=step7,
            step8=step8,
            translations_repaired=translations_repaired,
            audit_complete=True,
            from_cache=False,
            entity_context_hash=entity_hash,
            audit_cache_path=cache_path,
            journal_path=journal.path,
            r_editor=r_editor_report,
        )

    def _emit_progress(self, event: str, **fields: Any) -> None:
        progress = self._progress
        if progress is None:
            return
        emit = getattr(progress, "emit", None)
        if callable(emit):
            try:
                emit(event, **fields)
            except Exception:  # noqa: BLE001 — progress is diagnostics
                LOG.debug("B3: progress emit failed for %s", event, exc_info=True)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

# V4.2 R artifact schemas (identity-bearing, never reused across a schema
# change).
R_EDITED_SCHEMA = "pact-v4-translations-edited/v1"
R_CANDIDATES_SCHEMA = "pact-v4-edit-candidates/v1"


def _build_r_editor_report(
    *,
    cfg: B3AuditRepairConfig,
    outcome: Optional[RussianEditorOutcome],
    review_journal: Sequence[Mapping[str, Any]],
    from_cache: bool,
) -> Dict[str, Any]:
    """Build the Russian-editor report for the trial record / audit cache.

    ``outcome`` is None when the stage failed (transport/evaluator error) —
    the report then records status ``failed`` (the audit still protects the
    chapter; R edits were not applied). ``outcome.complete=False`` records
    status ``partial`` when at least one successful chunk produced edits /
    candidates (RESILIENCE t_406fc48c: per-chunk partial-apply) or
    ``incomplete`` when nothing was applied (all chunks failed — fail-closed
    with 0 successful output).
    """
    status = "disabled"
    outcome_payload: Optional[Dict[str, Any]] = None
    if cfg.russian_editor_enabled:
        if outcome is None:
            # RV fd7ee8e: enabled but the evaluator raised (transport or
            # evaluator error) — the stage FAILED, never "disabled". The
            # audit still protects the chapter; R edits were not applied.
            status = "failed"
        else:
            outcome_payload = outcome.to_payload()
            if outcome.complete:
                status = "complete"
            elif outcome.applied or outcome.candidates:
                status = "partial"
            else:
                status = "incomplete"
    report = {
        "enabled": cfg.russian_editor_enabled,
        "status": status,
        "version": cfg.russian_editor_version,
        "harness_version": cfg.russian_editor_harness_version,
        "chunk_size": cfg.russian_editor_chunk_size,
        "overlap_pairs": cfg.russian_editor_overlap_pairs,
        "safe_classes": sorted(cfg.russian_editor_safe_classes),
        "outcome": outcome_payload,
        "review_journal": [dict(entry) for entry in review_journal],
        "from_cache": from_cache,
    }
    return report


def _write_r_editor_artifacts(
    *,
    cfg: B3AuditRepairConfig,
    chapter_id: str,
    snapshot_hash: str,
    config_identity: str,
    out_dir: Path,
    edited_map: Mapping[str, str],
    candidates: Sequence[Any],
) -> None:
    """Persist the R-stage artifacts next to the audit cache.

    * ``translations_edited.json`` (schema ``pact-v4-translations-edited/v1``)
      — the R-EDITED map (raw + SAFE diff-gated edits) that the audit/repair
      consumed, with run identity;
    * ``edit_candidates.json`` (schema ``pact-v4-edit-candidates/v1``) — the
      REVIEW-classed candidates (pid, original, proposed, class, reason) that
      were forwarded to the B2 verifier, with run identity.

    Written ONLY on a cache miss (a full cache hit restores the repaired map
    and the report from the cache payload; the artifacts already exist from
    the original run — F8: the runner advertises only existing files).
    """
    identity = {
        "schema": R_EDITED_SCHEMA,
        "chapter_id": chapter_id,
        "snapshot_hash": snapshot_hash,
        "config_identity": config_identity,
    }
    _atomic_write_json(
        out_dir / "translations_edited.json",
        {**identity, "translations": dict(edited_map)},
    )
    candidate_payload = [
        {
            "pid": c.pid,
            "original": c.original,
            "proposed": c.proposed,
            "class": c.klass,
            "reason": c.reason,
        }
        for c in candidates
    ]
    _atomic_write_json(
        out_dir / "edit_candidates.json",
        {
            "schema": R_CANDIDATES_SCHEMA,
            "chapter_id": chapter_id,
            "snapshot_hash": snapshot_hash,
            "config_identity": config_identity,
            "candidates": candidate_payload,
        },
    )


def _reports_from_cache(
    *,
    cache: B3AuditCache,
    issue_count: int,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    repair_payload = cache.stored_repair()
    repair_complete = bool(
        repair_payload and repair_payload.get("repair_complete") is True
    )
    committed_pids = [
        pair[0] for pair in (repair_payload or {}).get("committed") or []
    ]
    passed_pids = list((repair_payload or {}).get("passed_pids") or [])
    debt_trace = list((repair_payload or {}).get("debt_trace") or [])
    # REPAIR-2 (t_768537b9): per-index non-fatal notices (no-op repairs
    # converted to per-index pass) restored from the cached repair payload.
    warnings = list((repair_payload or {}).get("warnings") or [])
    step6 = {
        "status": "complete",
        "audit_complete": True,
        "issue_count": issue_count,
        "from_cache": True,
    }
    step7 = {
        "status": (
            "complete"
            if repair_complete
            else ("incomplete" if repair_payload else "failed")
        ),
        "repair_complete": repair_complete,
        "committed_pids": committed_pids,
        "passed_pids": passed_pids,
        "debt_trace": debt_trace,
        "warnings": warnings,
        "from_cache": True,
    }
    # F1: the cache replay honors the same terminal gate as a live run — a
    # cached repair that did not complete is replayed as accepted_degraded /
    # NOT released, never silently upgraded to complete/released_as_audited.
    if repair_complete:
        step8 = {
            "status": "complete",
            "audit_complete": True,
            "released_as_audited": True,
            "from_cache": True,
        }
    else:
        step8 = {
            "status": "accepted_degraded",
            "audit_complete": True,
            "released_as_audited": False,
            "repair_complete": False,
            "reason": (
                "repair_failed" if repair_payload is None else "repair_incomplete"
            ),
            "debt_trace": debt_trace,
            "from_cache": True,
        }
    return step6, step7, step8


__all__ = [
    "B3_AUDIT_CACHE_SCHEMA",
    "B3_AUDIT_JOURNAL_SCHEMA",
    "B3AuditCache",
    "B3AuditRepair",
    "B3AuditRepairConfig",
    "B3AuditRepairResult",
    "AuditJournal",
    "render_entity_context_block",
    "R_EDITED_SCHEMA",
    "R_CANDIDATES_SCHEMA",
]
