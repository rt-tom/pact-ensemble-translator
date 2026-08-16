"""B2: selective repair (batch) + repair-as-verifier.

Canonical source: ``docs/plans/V4_1_AUDIT_B1_RU.md`` §5.2/§5.3 and §10 B2;
card t_73e190f7. Runs AFTER the B1 chunked audit and the B1.1 Tier A hard
filters (``pact_v4.audit.hard_filters.apply_hard_filters``, which already
returns CONFIRMED / REJECTED / TIER_B verdicts per issue).

Pipeline position::

    Qwen auditor (B1) -> hard filters (B1.1, Tier A) -> this module (B2)

Key rules implemented here (from the concept and the 2026-08-10 out-of-sample
review):

* **Repair-as-verifier (mandatory)** — the repair prompt tells the model the
  audit issue is a candidate, not an established fact; it must first verify
  against SOURCE and TRANSLATION, return PASS (no change) when the auditor is
  wrong, and repair only after confirming (``REPAIR_AS_VERIFIER_V1``).
* **Eligibility** — NOT a severity filter (Qwen severity is uncalibrated:
  real TPs are often minor). Tier A findings (``CONFIRMED``) repair directly;
  Tier B findings are eligible at ``confidence=high`` OR ``medium`` (owner
  decision 2026-08-13: the repair-as-verifier itself decides pass/repair —
  run_remote_001 sent 4 of 6 medium findings to debt unrepaired) AND within
  the allowed semantic categories (``changed_fact``/numeric claims are
  Tier A code-verified by B1.1, never guessed here). ``REJECTED`` findings
  are deterministic FPs — never repaired. Tier B below the eligibility bar
  goes to debt/diagnostic, never auto-repair.
* **Batch** — ONE call per group of eligible findings (not per-finding), each
  finding carrying an explicit ``[index]`` identifier; when eligible > 4 the
  group is split into microbatches of 3-4 (Cheng et al., Batch Prompting:
  quality degrades with batch size).
* **Cap** — max 100 eligible findings per chapter are repaired (configurable
  via ``findings_cap``, owner decision 2026-08-11 replacing the run_010
  10-finding cap that cut 73% of real findings); beyond the cap the findings
  go to debt tagged ``POLICY_LIMIT_TAG`` (analog of remote_budget).
* **Fail-closed** — a failed repair batch (transport error, invalid JSON,
  unknown/duplicate/missing index, invalid decision, pid mismatch, truncated
  repair) is debt, NEVER a silent PASS. A failed re-audit is debt, never
  ``0 findings``. REPAIR-2 (t_768537b9): a NO-OP repair (the model returned
  the current text with decision='repair') is NOT a batch failure — it is
  converted to a per-index PASS (journaled as a WARNING) so one no-op index
  cannot push the batch's real repairs into debt (run_013 batch1).
* **TEaR** — 0 eligible findings -> repair is skipped entirely (no model
  calls, ``skipped=True``, ``repair_complete=True``).
* **Single re-audit with bounded retry** — when at least one repair was
  committed, ONE Qwen call re-audits the changed PIDs + their neighbour
  window; the input is the FULL source + FULL current translation (every
  pair outside the reportable scope is marked CONTEXT_ONLY, frozen v4.1
  template); when the number of changed PIDs exceeds a threshold the re-audit
  covers the full chapter. The call is wrapped in a bounded B4 JSON retry
  (``reaudit_retry``, default 3 attempts): an empty/truncated body (run_010:
  Qwen returned 8265 tokens with ``content`` empty — reasoning-only answer)
  re-issues the identical request before the chapter is declared debt;
  transport failures are never retried here (B4 §1/§3).

Transport: the evaluator is backend-neutral over ``CompletionBackend`` (the
same boundary the B1 chunked audit uses). The lifecycle wrapper
(``pact_v4.runtime.model_lifecycle_adapters.LifecycleSelectiveRepairEvaluator``)
supplies the local ``llama-server`` backends and calls
``ModelRouter.ensure_resident`` before each phase (generator Gemma for the
repair batches, Qwen for the re-audit). This module deliberately never
imports ``pact_v4.runtime.model_lifecycle*`` (dual-mode rule; an import-guard
test enforces it).

This module is pure and deterministic except for the injected model calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import (
    AUDIT_V4_CATEGORIES,
    AUDIT_V4_CONFIDENCES,
    AUDIT_V4_SEVERITIES,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    MAX_OVERLAP_PAIRS,
    MIN_OVERLAP_PAIRS,
    AuditPair,
    audit_model_ref,
    build_greedy_chunks,
    get_overlap_context,
    pairs_from_maps,
    validate_chunk_json,
)
from pact_v4.audit.hard_filters import (
    B1_AUDIT_CATEGORIES,
    CONFIRMED,
    REJECTED,
    TIER_B,
    FilteredIssue,
)
from pact_v4.audit.russian_editor import ReviewCandidate
from pact_v4.runtime.backend_protocol import (
    JSON_OBJECT_SCHEMA,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    Message,
)
from pact_v4.runtime.backend_role_adapters import _reasoning_transported_via_request_options
from pact_v4.runtime.json_resilience import (
    EmptyResponseError,
    JsonRetryPolicy,
    TruncatedJSONError,
    extract_json_blocks,
    parse_json_response,
    retry_json_call,
)
from pact_v4.runtime.prompts_runtime import (
    DEFAULT_REPAIR_CONTEXT_WINDOW,
    DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY,
    REPAIR_AS_VERIFIER_V1,
    ReviewerPrompt,
    render_reaudit_prompt,
    render_selective_repair_prompt,
)
from pact_v4.runtime.reasoning_writer import append_error_marker, open_reasoning_writer

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen contract constants (concept §5.2/§5.3, §10 B2; owner decisions)
# ---------------------------------------------------------------------------

REPAIR_SCHEMA = "pact-repair/v1"
REPAIR_HARNESS_VERSION = "1.0"
# CANDIDATE-MERGE (t_0ffe56e1): v4 — REPAIR_AS_VERIFIER_V1 now tells the
# verifier the SOURCE of each finding (fidelity_auditor vs russian_editor)
# and that one PID may carry BOTH remarks to be resolved in ONE decision.
# Identity-bearing: the prompt version rides the run config identity, so a
# stale cached repaired map written under v3 can never replay under v4.
REPAIR_PROMPT_VERSION = "pact-v4-repair-as-verifier/v4"

# Cap on eligible findings repaired per chapter (owner decision 2026-08-11:
# run_010 showed the 10-finding cap cut 73% of real findings — cap on
# FINDINGS, not on calls; default raised 10->100, still configurable). Beyond
# the cap -> debt with the policy tag.
REPAIR_FINDINGS_CAP = 100
POLICY_LIMIT_TAG = "policy_limit: repair_findings_cap_100"

# Cheng et al. (Batch Prompting): batch quality degrades with size. Up to 4
# eligible findings -> one call; more -> microbatches of 3-4.
MICROBATCH_TRIGGER = 4
MICROBATCH_TARGET = 4

# Default neighbour window for the single re-audit (PIDs before/after each
# changed PID, in source order) — REPAIR-CTX (t_97b31f81): the re-audit is a
# CHUNKED audit over the affected region (changed + neighbours), NEVER the
# whole chapter (run_012 re-audit input was 41.5k tokens and truncated the
# 49k context). The whole-chapter mode (old ``full_threshold``) is CANCELLED.
DEFAULT_REAUDIT_NEIGHBOUR_WINDOW = 2

# REPAIR-CTX re-audit chunk settings (identity-bearing, F5): the re-audit
# reuses the audit's chunking/overlap mechanisms (build_greedy_chunks /
# get_overlap_context) over the affected region. Defaults mirror the audit
# chunk budget/overlap so ~50 pairs per chunk.
DEFAULT_REAUDIT_MAX_INPUT_TOKENS = DEFAULT_MAX_INPUT_TOKENS  # 3600
DEFAULT_REAUDIT_OVERLAP_TOKENS = DEFAULT_OVERLAP_TOKENS      # 400
DEFAULT_REAUDIT_MIN_OVERLAP_PAIRS = MIN_OVERLAP_PAIRS        # 2
DEFAULT_REAUDIT_MAX_OVERLAP_PAIRS = MAX_OVERLAP_PAIRS        # 6
# Version of the REPAIRED CHANGES delta block {pid, before, after} rendered
# into the re-audit prompt (identity-bearing — a format change invalidates
# cache/resume, F5).
REAUDIT_DELTA_FORMAT = "pact-v4-reaudit-delta/v1"

# CONTEXT-PID-DROP (RV3 t_c9eb65d4): the canonical fields of a journaled
# reaudit DROPPED issue object. ``validate_chunk_json`` accepts an
# otherwise well-formed context/foreign issue carrying an unknown EXTRA
# model field (it validates vocab/id, not the exact key set), so the fresh
# journaling retains ONLY these fields before attaching the harness
# ``_debug`` — the emitted/persisted dropped object then has exactly the
# ``_ISSUE_KEYS`` contract (these 6 + ``_debug``) that
# ``_validate_stage_progress`` enforces fail-closed on reload. A foreign
# key would otherwise reject the whole stage-progress cache on resume,
# losing the dropped diagnostic instead of preserving a valid object.
# CONTEXT-PID-DROP (RV4 t_cfb1523d): the SAME contract requires the
# canonical fields themselves to be structurally well-formed (non-empty
# string id/note/excerpt + valid vocab) — validate_chunk_json checks only
# id/category/severity/confidence before the scope drop, so a dropped
# issue missing/invalid note/excerpt is checked here at journal time
# (``_reaudit_dropped_issue_error``) and fails the chunk closed instead of
# being persisted malformed.
_REAUDIT_DROPPED_FIELDS = ("id", "category", "severity", "confidence", "note", "excerpt")


def _reaudit_dropped_issue_error(issue: Mapping[str, Any]) -> Optional[str]:
    """CONTEXT-PID-DROP (RV4 t_cfb1523d): return a reason string when a
    fresh scope-dropped issue is NOT a structurally well-formed canonical
    issue object, else None.

    ``validate_chunk_json`` scope-drops a context/foreign issue after
    checking only id/category/severity/confidence, so a dropped issue can
    still MISS note/excerpt or carry non-string canonical values. This
    mirrors the exact persisted contract ``_validate_stage_progress``
    enforces on reloaded dropped objects (non-empty string id/note/excerpt,
    valid category/severity/confidence vocab), reporting such an issue at
    journal time — the caller then fails closed WITHOUT persisting the
    malformed dropped object, instead of writing a key-set/value violation
    into stage_progress that full-misses the whole cache on the next
    resume (the dropped diagnostic would be lost).
    """
    if not isinstance(issue, dict):
        return "not an object"
    pid = issue.get("id")
    if not isinstance(pid, str) or not pid:
        return "id is missing or not a non-empty string"
    if issue.get("category") not in AUDIT_V4_CATEGORIES:
        return f"invalid category {issue.get('category')!r}"
    if issue.get("severity") not in AUDIT_V4_SEVERITIES:
        return f"invalid severity {issue.get('severity')!r}"
    if issue.get("confidence") not in AUDIT_V4_CONFIDENCES:
        return f"invalid confidence {issue.get('confidence')!r}"
    note = issue.get("note")
    if not isinstance(note, str) or not note.strip():
        return "note is missing, not a string, or empty"
    excerpt = issue.get("excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return "excerpt is missing, not a string, or empty"
    return None

# Repair output budget (per batch call) and re-audit output budget. REPAIR-CTX
# (t_97b31f81): the re-audit input is now the affected REGION (changed PIDs +
# neighbours, chunked), not the full chapter — but the 20000-token output
# budget is kept (chunked JSON responses + reasoning headroom; the old
# 12000-token budget was exhausted by reasoning on the full input in
# run_010-style chapters — owner decision 2026-08-11).
# REPAIR-MAX-TOKENS (owner decision 2026-08-15, "16к Делай"): 4000 → 16000.
# run_0004-0005 remote (deepseek + reasoning high): repair batches burned
# 8-33k BYTES of reasoning (b3_repair_batch1_raw.txt=0, reasoning=26445)
# and exhausted the 4000-token budget BEFORE emitting JSON → empty/truncated
# responses → 5/6 batches failed → chapter accepted_degraded (repair
# incomplete). 16000 = reasoning headroom (deepseek regularly thinks 2-9k
# tokens) + JSON content, mirroring the audit's own budget logic.
DEFAULT_REPAIR_MAX_TOKENS = 16000
DEFAULT_REAUDIT_MAX_TOKENS = 20000

# REPAIR-ROBUST (card t_b6fd6cbd, run_0005): the per-batch repair reasoning
# effort for REMOTE transports. run_0005 batch1: deepseek (max variant)
# burned 32k tokens of reasoning on a repair batch and exhausted max_tokens
# BEFORE emitting content (raw=0, finish=length) — the default is low (1),
# high reasoning on a mechanical repair pass is always excessive. The
# Evaluator sends it via request_options ONLY when the backend transports
# reasoning that way (_reasoning_transported_via_request_options): local
# llama-server transports receive the budget from their server args
# (--reasoning-budget) and LocalOpenAIBackend rejects request_options, so
# the field is inert locally (owner rule: local servers always run with the
# same args). Identity-bearing (F5): a change must invalidate a stale
# cached repaired map, so it rides the config chain
# (StrictRunConfig -> B3AuditRepairConfig -> SelectiveRepairConfig).
DEFAULT_REPAIR_REASONING = 1

# REPAIR-ROBUST (card t_b6fd6cbd): the tolerant repair parser's minimum
# coverage — the fraction of a batch's findings that must be recovered from
# a "dirty" response for the batch to be GOOD. Below it the batch FAILS as
# before: accepting 1 record of 4 (25%) is a sign of serious corruption,
# not salvage. run_0005 batches recovered 15/15 (100%) via block
# extraction, so the default 50% only guards against accepting garbage.
REPAIR_PARSE_MIN_COVERAGE = 0.5

# The re-audit retry policy is identity-bearing (F5: a policy change must
# invalidate a stale cached repaired map), so the production config chain
# (StrictRunConfig -> B3AuditRepairConfig -> SelectiveRepairConfig) carries
# the policy as scalars. The defaults are pinned to the JsonRetryPolicy
# class defaults (B4 §5: max_retries=2 -> 3 attempts, base_delay_seconds=1.0)
# so the identity mirrors the effective runtime policy without drift.
DEFAULT_REAUDIT_MAX_RETRIES = JsonRetryPolicy.max_retries
DEFAULT_REAUDIT_BASE_DELAY_SECONDS = JsonRetryPolicy.base_delay_seconds


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectiveRepairConfig:
    """Settings for one selective-repair run (frozen contract of the run)."""

    findings_cap: int = REPAIR_FINDINGS_CAP
    microbatch_trigger: int = MICROBATCH_TRIGGER
    microbatch_target: int = MICROBATCH_TARGET
    allowed_categories: frozenset = frozenset(B1_AUDIT_CATEGORIES)
    template: ReviewerPrompt = REPAIR_AS_VERIFIER_V1
    max_tokens: int = DEFAULT_REPAIR_MAX_TOKENS
    label: str = "phase3/selective_repair_v4"
    harness_version: str = REPAIR_HARNESS_VERSION
    prompt_version: str = REPAIR_PROMPT_VERSION
    # REPAIR-ROBUST (card t_b6fd6cbd, run_0005, F5): the per-batch repair
    # reasoning effort (0=off, 1=low, 2=medium, 3=high) for REMOTE
    # transports only. Default 1 (low) — deepseek high burns 32k reasoning
    # tokens on a repair batch and exhausts max_tokens before content
    # (run_0005 batch1: raw=0, finish=length). Identity-bearing: wired from
    # the run config (StrictRunConfig -> B3AuditRepairConfig), so a change
    # invalidates a stale cached repaired map.
    repair_reasoning: Optional[int] = DEFAULT_REPAIR_REASONING
    # REPAIR-CTX (card t_97b31f81, owner decision 2026-08-12): local context
    # window for repair batches — ONLY the findings PIDs plus ±N neighbour
    # pairs are rendered (NOT the full chapter maps). Default ±3 (3 назад +
    # 3 вперёд). Identity-bearing (F5): a window change must invalidate a
    # stale cached repaired map.
    repair_context_window: int = DEFAULT_REPAIR_CONTEXT_WINDOW
    # REPAIR-2 (card t_768537b9, owner decision 2026-08-12): per-category
    # window overrides — {category: window}; a category not in the map falls
    # back to ``repair_context_window``. Default widens
    # invented_gender/referent/omission to ±10 (gender/referent judgments
    # need the FAR referent — run_013 p00193's female referent sat 7 PIDs
    # away, outside ±3, and the repair wrongly changed внучка→внук);
    # changed_fact/addition stay ±3 (local edit). Identity-bearing (F5): the
    # per-category windows ride the run config identity via
    # ``StrictRunConfig.audit_repair_context_window_by_category``, so a
    # change invalidates a stale cached repaired map.
    repair_context_window_by_category: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY)
    )
    reaudit_enabled: bool = True
    reaudit_neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW
    # REPAIR-CTX (t_97b31f81): the re-audit is a CHUNKED audit over the
    # affected region (changed + neighbours) reusing the audit's chunking /
    # overlap mechanisms — the whole-chapter re-audit mode is CANCELLED, so
    # there is no full_threshold anymore. Chunk/overlap settings and the
    # REPAIRED CHANGES delta format are identity-bearing (F5): changing them
    # invalidates a stale cached repaired map.
    reaudit_max_input_tokens: int = DEFAULT_REAUDIT_MAX_INPUT_TOKENS
    reaudit_overlap_tokens: int = DEFAULT_REAUDIT_OVERLAP_TOKENS
    reaudit_min_overlap_pairs: int = DEFAULT_REAUDIT_MIN_OVERLAP_PAIRS
    reaudit_max_overlap_pairs: int = DEFAULT_REAUDIT_MAX_OVERLAP_PAIRS
    reaudit_delta_format: str = REAUDIT_DELTA_FORMAT
    reaudit_max_tokens: int = DEFAULT_REAUDIT_MAX_TOKENS
    # Bounded B4 JSON retry for the re-audit call (owner decision 2026-08-11,
    # run_010: a single empty content on the full-input re-audit failed the
    # chapter closed). Default 3 attempts; transport failures are never
    # retried here (B4 §1/§3) — they surface as failed re-audit debt.
    reaudit_retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)
    reaudit_label: str = "phase3/reaudit_scope_v4"


@dataclass(frozen=True)
class EligibleFinding:
    """One finding admitted to a repair batch.

    ``index`` is the explicit ``[index]`` identifier the model answers by
    (Cheng et al. contract); ``tier`` is ``"A"`` (CONFIRMED — repair
    directly) or ``"B"`` (CANDIDATE — verify-before-repair).

    CANDIDATE-MERGE (t_0ffe56e1): ``source_stage`` names the stage that
    produced the remark — ``"fidelity_auditor"`` (B1 audit finding) or
    ``"russian_editor"`` (R-editor REVIEW candidate). A MERGED finding (one
    PID with several remarks — same-stage or cross-stage alike, owner
    clarification 2026-08-13) joins the distinct stage labels with ``"+"``
    (``source_stage="fidelity_auditor+russian_editor"``) and carries the
    per-source remarks in ``sources`` (each with its own stage/tier/category/
    severity/confidence/note/excerpt/issue) so the repair model sees ALL
    remarks of the PID in ONE call and builds ONE decision. After the merge
    every PID appears at most once in a batch (indices unique 1..N).
    """

    index: int
    pid: str
    tier: str
    category: str
    severity: str
    confidence: str
    note: str
    excerpt: str
    issue: Mapping[str, Any]
    source_stage: str = "fidelity_auditor"
    sources: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RepairResult:
    """One parsed per-index decision of a repair batch response."""

    index: int
    decision: str  # "pass" | "repair"
    pid: str = ""
    repaired_translation: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RepairBatchOutcome:
    """Outcome of one repair batch (one model call)."""

    batch_index: int
    status: str  # "GOOD" | "PARTIAL" | "FAILED"
    findings: Tuple[EligibleFinding, ...] = ()
    results: Tuple[RepairResult, ...] = ()
    error: str = ""
    # REPAIR-2 (t_768537b9): per-index NON-FATAL notices journaled with the
    # batch — e.g. no-op repairs converted to per-index pass (the batch stays
    # GOOD when they are the only issue). Never a batch-killing error.
    warnings: Tuple[str, ...] = ()
    # REPAIR-ROBUST-PARTIAL (t_c0cb8e3c): finding indices the TOLERANT
    # salvage did NOT recover (``seen != expected``, coverage >= 50%) — the
    # batch is PARTIAL: the valid recovered repairs are retained, the missing
    # findings are routed to debt by the evaluator, and ``repair_complete``
    # can never become True (a partial response is never published complete).
    missing_indices: Tuple[int, ...] = ()


@dataclass(frozen=True)
class ReauditOutcome:
    """Outcome of the post-repair re-audit pass.

    REPAIR-CTX (t_97b31f81): the re-audit is a CHUNKED audit over the
    affected region (changed PIDs + neighbours), never the whole chapter.
    ``complete`` is True when EVERY chunk's call, JSON parse and scope
    validation succeeded (``issues`` then carries what the re-audit found in
    scope across all chunks); ``failed`` is True when any chunk failed — the
    caller must treat that as debt and NEVER claim ``0 findings``
    (fail-closed, Phase 0/A1c fix). ``full`` is retained for journal schema
    stability and is ALWAYS False (the whole-chapter re-audit mode is
    cancelled).
    """

    complete: bool
    failed: bool
    issues: Tuple[Mapping[str, Any], ...] = ()
    scope: Tuple[str, ...] = ()
    full: bool = False  # always False — whole-chapter mode cancelled
    reason: str = ""


@dataclass(frozen=True)
class RepairedChange:
    """One repair delta entry for the re-audit prompt (REPAIRED CHANGES).

    ``pid``, ``before`` (pre-repair translation), ``after`` (repaired
    translation) — the auditor verifies the CORRECTNESS of each repair
    against the delta instead of just re-reading the text.
    """

    pid: str
    before: str
    after: str


@dataclass(frozen=True)
class SelectiveRepairOutcome:
    """Aggregated result of a selective-repair pass (``schema: pact-repair/v1``)."""

    schema: str
    harness_version: str
    prompt_version: str
    model: str
    eligible_count: int
    capped: Tuple[EligibleFinding, ...]
    rejected: Tuple[FilteredIssue, ...]
    ineligible: Tuple[EligibleFinding, ...]
    batches: Tuple[RepairBatchOutcome, ...]
    committed: Tuple[Tuple[str, str], ...]  # (pid, repaired_text)
    passed_pids: Tuple[str, ...]
    debt_trace: Tuple[str, ...]
    reaudit: Optional[ReauditOutcome]
    repair_complete: bool
    skipped: bool  # TEaR: 0 eligible findings -> repair skipped entirely
    # REPAIR-2 (t_768537b9): non-fatal per-index notices aggregated from the
    # batches (no-op repairs converted to pass) — journaled with the repair
    # round (audit_journal repair_round event), never batch-fatal.
    warnings: Tuple[str, ...] = ()
    # V4.2 R: accept/reject journal for the Russian-editor REVIEW candidates
    # verified in this pass (one entry per candidate: pid, class, original,
    # proposed, verdict accepted|rejected|failed, reason, committed text).
    review_journal: Tuple[Dict[str, Any], ...] = ()

    def to_payload(self) -> dict:
        return {
            "schema": self.schema,
            "harness_version": self.harness_version,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "eligible_count": self.eligible_count,
            "capped": [dict(f.issue) for f in self.capped],
            "rejected": [
                {"id": f.issue.get("id"), "verdict": f.verdict, "reason": f.reason}
                for f in self.rejected
            ],
            "ineligible": [dict(f.issue) for f in self.ineligible],
            "batches": [
                {
                    "batch_index": b.batch_index,
                    "status": b.status,
                    "findings": [
                        {
                            "index": f.index,
                            "pid": f.pid,
                            "tier": f.tier,
                            "category": f.category,
                            "severity": f.severity,
                            "confidence": f.confidence,
                            # CANDIDATE-MERGE (t_0ffe56e1): the finding's
                            # source stage(s) + merged per-source remarks
                            # (journal visibility for the trial record).
                            "source_stage": f.source_stage,
                            "sources": [dict(s) for s in f.sources],
                        }
                        for f in b.findings
                    ],
                    "results": [
                        {
                            "index": r.index,
                            "decision": r.decision,
                            "pid": r.pid,
                            "repaired_translation": r.repaired_translation,
                            "reason": r.reason,
                        }
                        for r in b.results
                    ],
                    "error": b.error,
                    # REPAIR-2 (t_768537b9): per-index non-fatal notices.
                    "warnings": list(b.warnings),
                    # REPAIR-ROBUST-PARTIAL (t_c0cb8e3c): indices the tolerant
                    # salvage did not recover — surfaced in diagnostics so a
                    # partial batch is never mistaken for complete.
                    "missing_indices": list(b.missing_indices),
                }
                for b in self.batches
            ],
            "committed": [list(pair) for pair in self.committed],
            "passed_pids": list(self.passed_pids),
            "debt_trace": list(self.debt_trace),
            # REPAIR-2 (t_768537b9): aggregated non-fatal per-index notices.
            "warnings": list(self.warnings),
            "reaudit": (
                {
                    "complete": self.reaudit.complete,
                    "failed": self.reaudit.failed,
                    "issues": [dict(i) for i in self.reaudit.issues],
                    "scope": list(self.reaudit.scope),
                    "full": self.reaudit.full,
                    "reason": self.reaudit.reason,
                }
                if self.reaudit is not None
                else None
            ),
            "repair_complete": self.repair_complete,
            "skipped": self.skipped,
            "review_journal": [dict(entry) for entry in self.review_journal],
        }


# Roles that may serve the repair call, in priority order (generator first —
# the repair model is the generator by owner decision, Kocmi-safe).
_REPAIR_ROLES = ("generator", "default")


def repair_model_ref(backend: CompletionBackend) -> str:
    """Resolve the model reference for the repair role (generator, else
    ``default``); raises when unbound so a misconfigured role fails loudly."""
    bindings = backend.descriptor.model_bindings
    for role in _REPAIR_ROLES:
        ref = bindings.get(role)
        if ref:
            return str(ref)
    raise ValueError(
        f"no model binding for repair role(s) {list(_REPAIR_ROLES)!r}; "
        f"backend model_bindings={dict(bindings)!r}"
    )


# ---------------------------------------------------------------------------
# Eligibility (pure, deterministic)
# ---------------------------------------------------------------------------


def select_eligible(
    filtered: Sequence[FilteredIssue],
    *,
    allowed_categories: frozenset = frozenset(B1_AUDIT_CATEGORIES),
) -> Tuple[Tuple[EligibleFinding, ...], Tuple[FilteredIssue, ...], Tuple[EligibleFinding, ...]]:
    """Split hard-filter verdicts into (eligible, rejected, ineligible).

    * ``CONFIRMED`` (Tier A) — always eligible, tier ``"A"`` (repair directly;
      the code already proved the issue, no re-verification needed).
    * ``TIER_B`` — eligible (tier ``"B"``, verify-before-repair) ONLY when
      ``confidence`` is ``high`` or ``medium`` AND the category is in
      ``allowed_categories`` (owner decision 2026-08-13: medium-confidence
      findings also go to the repair-as-verifier, which itself decides
      pass/repair — run_remote_001 sent 4 of 6 medium findings to debt
      unrepaired; severity is NOT an eligibility filter — real TPs are
      often minor, severity stays in the journal only).
    * ``REJECTED`` — deterministic false positive: never repaired.
    * ``TIER_B`` below the bar (low confidence or category outside the
      allowed set) — ineligible: debt/diagnostic, never auto-repair.

    ``index`` here is the batch-local explicit ``[index]`` identifier,
    assigned in source order starting at 1 (re-numbered per batch later).
    """
    eligible: list = []
    rejected: list = []
    ineligible: list = []
    for position, f in enumerate(filtered, start=1):
        issue = f.issue
        pid = str(issue.get("id", ""))
        category = str(issue.get("category", ""))
        severity = str(issue.get("severity", ""))
        confidence = str(issue.get("confidence", ""))
        if f.verdict == CONFIRMED:
            eligible.append(
                EligibleFinding(
                    index=position,
                    pid=pid,
                    tier="A",
                    category=category,
                    severity=severity,
                    confidence=confidence,
                    note=str(issue.get("note", "")),
                    excerpt=str(issue.get("excerpt", "")),
                    issue=issue,
                    # CANDIDATE-MERGE (t_0ffe56e1): audit findings carry the
                    # fidelity-auditor stage label (the repair prompt renders
                    # it so the verifier applies the right contract).
                    source_stage=f.source_stage,
                )
            )
        elif f.verdict == REJECTED:
            rejected.append(f)
        else:  # TIER_B
            # Owner decision 2026-08-13: medium-confidence findings are
            # eligible too — the repair-as-verifier decides pass/repair
            # (run_remote_001: 4 of 6 findings with confidence=medium went
            # to debt unrepaired because eligibility required high).
            if confidence in ("high", "medium") and category in allowed_categories:
                eligible.append(
                    EligibleFinding(
                        index=position,
                        pid=pid,
                        tier="B",
                        category=category,
                        severity=severity,
                        confidence=confidence,
                        note=str(issue.get("note", "")),
                        excerpt=str(issue.get("excerpt", "")),
                        issue=issue,
                        source_stage=f.source_stage,
                    )
                )
            else:
                ineligible.append(
                    EligibleFinding(
                        index=position,
                        pid=pid,
                        tier="B",
                        category=category,
                        severity=severity,
                        confidence=confidence,
                        note=str(issue.get("note", "")),
                        excerpt=str(issue.get("excerpt", "")),
                        issue=issue,
                        source_stage=f.source_stage,
                    )
                )
    return tuple(eligible), tuple(rejected), tuple(ineligible)


def apply_findings_cap(
    eligible: Sequence[EligibleFinding],
    cap: int = REPAIR_FINDINGS_CAP,
) -> Tuple[Tuple[EligibleFinding, ...], Tuple[EligibleFinding, ...]]:
    """Cap eligible findings per chapter; beyond -> ``(kept, capped)``.

    The kept set keeps the original order; the capped findings go to debt with
    ``POLICY_LIMIT_TAG`` (owner decision 2026-08-08). Tier A findings are
    kept before Tier B ones so code-confirmed issues are never displaced by
    semantic candidates at the cap boundary.
    """
    ordered = sorted(
        enumerate(eligible), key=lambda item: (0 if item[1].tier == "A" else 1, item[0])
    )
    kept_indexes = sorted(i for i, _ in ordered[:cap])
    capped_indexes = sorted(i for i, _ in ordered[cap:])
    return (
        tuple(eligible[i] for i in kept_indexes),
        tuple(eligible[i] for i in capped_indexes),
    )


def make_microbatches(
    eligible: Sequence[EligibleFinding],
    *,
    trigger: int = MICROBATCH_TRIGGER,
    target: int = MICROBATCH_TARGET,
) -> Tuple[Tuple[EligibleFinding, ...], ...]:
    """Split eligible findings into one call per group (Cheng et al.).

    Up to ``trigger`` (4) findings -> a single batch (one call). Above the
    trigger the group is split into microbatches of ~``target`` (3-4): the
    number of batches is ``ceil(n / target)`` and findings are distributed as
    evenly as possible so every batch stays within 3-4 items (never 1-2
    except when the total itself is smaller than the trigger). Each finding
    keeps its explicit ``[index]`` (re-numbered 1..N within its batch).
    """
    n = len(eligible)
    if n <= trigger:
        return (tuple(eligible),)
    n_batches = (n + target - 1) // target
    base, extra = divmod(n, n_batches)
    batches: list = []
    cursor = 0
    for b in range(n_batches):
        size = base + (1 if b < extra else 0)
        chunk = list(eligible[cursor : cursor + size])
        cursor += size
        for i, finding in enumerate(chunk, start=1):
            chunk[i - 1] = EligibleFinding(
                index=i,
                pid=finding.pid,
                tier=finding.tier,
                category=finding.category,
                severity=finding.severity,
                confidence=finding.confidence,
                note=finding.note,
                excerpt=finding.excerpt,
                issue=finding.issue,
                # CANDIDATE-MERGE (t_0ffe56e1): the source stage and the
                # merged per-source remarks survive the microbatch
                # renumbering — the model must still see them per index.
                source_stage=finding.source_stage,
                sources=finding.sources,
            )
        batches.append(tuple(chunk))
    return tuple(batches)


# ---------------------------------------------------------------------------
# Response parsing (fail-closed)
# ---------------------------------------------------------------------------


def parse_repair_batch(
    text: str,
    findings: Sequence[EligibleFinding],
    current_by_pid: Mapping[str, str],
) -> Tuple[Tuple[RepairResult, ...], Tuple[str, ...], Tuple[str, ...]]:
    """Parse and strictly validate one repair batch response.

    Fail-closed contract: the response must be a JSON object with a
    ``results`` array; every finding ``[index]`` must be answered exactly
    once; each result must carry ``decision`` ``pass``|``repair``; a repair
    must name the EXACT PID of the finding that ``index`` refers to (the
    index/PID contract — a repair naming any other batch target would commit
    the fix to the wrong paragraph) and a NON-EMPTY ``repaired_translation``.
    When several findings share one PID each index is still validated against
    its own finding's PID, so a shared-PID group is answered per index exactly
    like a distinct-PID one.

    REPAIR-2 (card t_768537b9, run_013): a "repair" whose text equals the
    current text is a NO-OP — it is converted to a per-index PASS (``decision=
    "pass"``, ``reason="no-op repair converted to pass"``) and reported in the
    returned ``warnings``, it does NOT fail the batch (run_013 batch1: one
    no-op index killed the whole batch and pushed 4 real findings into debt).
    If EVERY index of the batch is a no-op the batch is GOOD with no repairs
    committed (the model honestly decided nothing needed changing).

    REPAIR-ROBUST (card t_b6fd6cbd, run_0005): the NORMAL path is unchanged —
    ``parse_json_response`` accepts a complete ``{"results": [...]}`` object
    (fences/prose tolerated as before). Only when the strict parse FAILS does
    the tolerant parser take over (``_parse_repair_tolerant``): a top-level
    LIST (batch2) is wrapped as ``{"results": list}`` and validated like the
    normal path; a TRUNCATED outer object (batch3/5/7/9: the model dropped
    the final ``}``) has every balanced ``{..}`` record extracted
    string-aware and each complete record accepted individually, with a
    coverage gate (< ``REPAIR_PARSE_MIN_COVERAGE`` of findings recovered →
    the batch FAILS as before, never 1 record of 4).

    REPAIR-ROBUST-PARTIAL (t_c0cb8e3c): the 50% salvage threshold is a
    RECOVERY threshold, never a publication-complete threshold. A tolerant
    response that recovered SOME but not ALL findings (``seen != expected``,
    coverage >= 50%) still returns its valid recovered records (they are
    committed), but the caller marks the batch PARTIAL and routes every
    missing index to debt — a partial response must never set
    ``repair_complete=True`` or ``released_as_audited=True``. A warning alone
    is not sufficient evidence for a fail-closed repair/release gate.

    Returns ``(results, errors, warnings)``:
    * ``errors`` — FATAL for the batch (debt, never a silent PASS): invalid
      JSON, unknown/duplicate/missing index, invalid decision, repair pid not
      matching the finding's pid, empty repaired_translation, truncated
      repair. ANY error fails the whole batch.
    * ``warnings`` — per-index NON-FATAL notices (no-op repairs converted to
      pass, tolerant-parse skips); the batch stays GOOD when they are the
      only issue.
    """
    errors: list = []
    warnings: list = []
    try:
        parsed = parse_json_response(text)
    except Exception as exc:
        # REPAIR-ROBUST: the strict parse failed (top-level LIST, truncated
        # object, empty/broken body) — try the tolerant salvage path.
        return _parse_repair_tolerant(text, findings, current_by_pid, exc)
    if not isinstance(parsed, dict) or "results" not in parsed:
        return (), ("root object has no 'results' array",), ()
    results = parsed.get("results")
    if not isinstance(results, list):
        return (), ("'results' is not an array",), ()
    expected = {f.index for f in findings}
    finding_by_index = {f.index: f for f in findings}
    seen: set = set()
    out: list = []
    for item in results:
        if not isinstance(item, dict):
            errors.append("result entry is not an object")
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            errors.append(f"result entry has invalid index {item.get('index')!r}")
            continue
        if index not in expected:
            errors.append(f"unknown finding index {index}")
            continue
        if index in seen:
            errors.append(f"duplicate finding index {index}")
            continue
        seen.add(index)
        decision = item.get("decision")
        if decision not in ("pass", "repair"):
            errors.append(f"index {index}: invalid decision {decision!r}")
            continue
        reason = str(item.get("reason", ""))
        if decision == "pass":
            out.append(RepairResult(index=index, decision="pass", reason=reason))
            continue
        pid = item.get("pid")
        repaired = item.get("repaired_translation")
        expected_pid = finding_by_index[index].pid
        if not isinstance(pid, str) or pid != expected_pid:
            errors.append(
                f"index {index}: repair pid {pid!r} does not match finding "
                f"pid {expected_pid!r} (index/PID contract)"
            )
            continue
        if not isinstance(repaired, str) or not repaired.strip():
            errors.append(f"index {index}: repair has empty repaired_translation")
            continue
        if repaired.strip() == str(current_by_pid.get(pid, "")).strip():
            # REPAIR-2 (card t_768537b9, run_013 batch1): a no-op "repair"
            # (model returned the current text with decision='repair') is
            # converted to a per-index PASS — the model effectively decided
            # nothing needed changing. NOT a batch-killing error: run_013's
            # single no-op index failed the whole batch and pushed 4 real
            # findings (p00016/p00033/p00035/p00080) into debt. The WARNING
            # is journaled (RepairBatchOutcome.warnings) so the operator sees
            # the model misused the decision contract.
            warnings.append(
                f"index {index}: no-op repair converted to pass "
                f"(repaired_translation equals the current text for pid {pid})"
            )
            out.append(
                RepairResult(
                    index=index,
                    decision="pass",
                    pid=pid,
                    reason="no-op repair converted to pass",
                )
            )
            continue
        # B3 (run_011): text-preservation gate — a repair that keeps under 40%
        # of the current text is a TRUNCATED repair (the model returned a
        # fragment instead of the FULL corrected PID; 7 PIDs in run_011 lost
        # dialogues/sentences this way). One-directional length guard, NOT
        # two-way similarity — a legitimate fix may rewrite heavily but must
        # keep the whole paragraph.
        current_text = str(current_by_pid.get(pid, ""))
        if len(repaired.strip()) < 0.4 * len(current_text.strip()):
            errors.append(
                f"index {index}: truncated repair — repaired_translation is "
                f"{len(repaired.strip())} chars vs {len(current_text.strip())} "
                f"chars current text (<40% preserved; the FULL corrected PID "
                f"text must be returned, never a fragment)"
            )
            continue
        out.append(
            RepairResult(
                index=index,
                decision="repair",
                pid=pid,
                repaired_translation=repaired,
                reason=reason,
            )
        )
    missing = sorted(expected - seen)
    if missing:
        errors.append(f"missing answer(s) for finding index(es) {missing}")
    return tuple(out), tuple(errors), tuple(warnings)


def _parse_repair_tolerant(
    text: str,
    findings: Sequence[EligibleFinding],
    current_by_pid: Mapping[str, str],
    exc: Exception,
) -> Tuple[Tuple[RepairResult, ...], Tuple[str, ...], Tuple[str, ...]]:
    """REPAIR-ROBUST (card t_b6fd6cbd, run_0005): salvage a dirty batch body.

    Two failure classes the strict parser cannot accept:

    * **Top-level LIST** (batch2): valid JSON, but the model returned
      ``[...]`` instead of ``{"results": [...]}`` — wrap and validate with
      the SAME strict per-record rules as the normal dict path.
    * **Truncated outer object** (batch3/5/7/9): the model dropped the final
      ``}`` (``...}]`` instead of ``...}]}``) — every balanced ``{..}`` block
      is extracted string-aware (``extract_json_blocks``) and each COMPLETE
      repair record is accepted individually.

    Fail-closed PER RECORD (never the whole batch for one bad record): only
    complete records are accepted — a record with a broken index/PID
    contract, an invalid decision, an empty repair or a truncated repair is
    SKIPPED with a warning (the index/PID contract, no-op conversion and 40%
    gate are the same rules as the strict path, so a record accepted through
    either path passes the same gates). The COVERAGE GATE still fails the
    batch when fewer than ``REPAIR_PARSE_MIN_COVERAGE`` of the findings were
    recovered (accepting 1 record of 4 is a sign of serious corruption, not
    salvage).

    REPAIR-ROBUST-PARTIAL (t_c0cb8e3c): when coverage >= 50% but ``seen !=
    expected`` (the model answered only part of the batch), the recovered
    records are still returned (``errors`` stays empty — the salvage policy
    is preserved), but the caller (``SelectiveRepairEvaluator._run_batch``)
    marks the batch PARTIAL and routes every missing finding index to debt.
    The 50% threshold is a RECOVERY floor, NOT a publication-complete
    threshold: a partial response must never set ``repair_complete=True`` or
    ``released_as_audited=True`` (a warning is not sufficient evidence for a
    fail-closed repair/release gate).
    """
    errors: list = []
    warnings: list = []
    # 2. Valid JSON, top-level LIST (batch2): wrap and validate like the
    #    normal path (fail-closed per-record rules unchanged).
    try:
        direct = json.loads(text.strip().lstrip("\ufeff"))
    except json.JSONDecodeError:
        direct = None
    if isinstance(direct, list):
        return parse_repair_batch(
            json.dumps({"results": direct}, ensure_ascii=False),
            findings,
            current_by_pid,
        )
    # 3. Truncated outer object (batch3/5/7/9) / prose / fences: extract
    #    every balanced {..} block and validate each record individually.
    expected = {f.index for f in findings}
    finding_by_index = {f.index: f for f in findings}
    seen: set = set()
    out: list = []
    for block in extract_json_blocks(text):
        try:
            item = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            warnings.append(f"tolerant parse skipped a non-object block")
            continue
        # Same per-record rules as the strict path; violations SKIP the
        # record (fail-closed per record, never the whole batch).
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            warnings.append(
                f"tolerant parse skipped a record with invalid index "
                f"{item.get('index')!r}"
            )
            continue
        if index not in expected:
            warnings.append(f"tolerant parse skipped unknown finding index {index}")
            continue
        if index in seen:
            warnings.append(f"tolerant parse skipped duplicate finding index {index}")
            continue
        decision = item.get("decision")
        if decision not in ("pass", "repair"):
            warnings.append(f"index {index}: invalid decision {decision!r}")
            continue
        reason = str(item.get("reason", ""))
        if decision == "pass":
            seen.add(index)
            out.append(RepairResult(index=index, decision="pass", reason=reason))
            continue
        pid = item.get("pid")
        repaired = item.get("repaired_translation")
        expected_pid = finding_by_index[index].pid
        if not isinstance(pid, str) or pid != expected_pid:
            warnings.append(
                f"index {index}: repair pid {pid!r} does not match finding "
                f"pid {expected_pid!r} (index/PID contract)"
            )
            continue
        if not isinstance(repaired, str) or not repaired.strip():
            warnings.append(f"index {index}: repair has empty repaired_translation")
            continue
        if repaired.strip() == str(current_by_pid.get(pid, "")).strip():
            warnings.append(
                f"index {index}: no-op repair converted to pass "
                f"(repaired_translation equals the current text for pid {pid})"
            )
            seen.add(index)
            out.append(
                RepairResult(
                    index=index,
                    decision="pass",
                    pid=pid,
                    reason="no-op repair converted to pass",
                )
            )
            continue
        current_text = str(current_by_pid.get(pid, ""))
        if len(repaired.strip()) < 0.4 * len(current_text.strip()):
            warnings.append(
                f"index {index}: truncated repair skipped — repaired_translation "
                f"is {len(repaired.strip())} chars vs {len(current_text.strip())} "
                f"chars current text (<40% preserved)"
            )
            continue
        seen.add(index)
        out.append(
            RepairResult(
                index=index,
                decision="repair",
                pid=pid,
                repaired_translation=repaired,
                reason=reason,
            )
        )
    missing = sorted(expected - seen)
    if missing:
        warnings.append(
            f"tolerant parse: no answer recovered for finding index(es) {missing}"
        )
    # 5. Coverage gate: < REPAIR_PARSE_MIN_COVERAGE of findings recovered is
    #    serious corruption — the batch FAILS as before.
    coverage = len(seen) / len(expected) if expected else 1.0
    if coverage < REPAIR_PARSE_MIN_COVERAGE:
        return (), (
            f"response is not valid JSON: {exc}; tolerant parse recovered "
            f"{len(seen)}/{len(expected)} finding(s) "
            f"(<{int(REPAIR_PARSE_MIN_COVERAGE * 100)}% coverage)",
        ), tuple(warnings)
    return tuple(out), (), tuple(warnings)


# ---------------------------------------------------------------------------
# Re-audit scope planning (pure)
# ---------------------------------------------------------------------------


def plan_reaudit_scope(
    changed_pids: Sequence[str],
    all_pids: Sequence[str],
    *,
    neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW,
) -> Tuple[str, ...]:
    """Scope of the post-repair re-audit: changed PIDs + their neighbours.

    REPAIR-CTX (t_97b31f81, owner decision 2026-08-12): the re-audit covers
    ONLY the affected region — the changed PIDs plus ``neighbour_window``
    PIDs before/after each in source order, in source order. The whole-
    chapter re-audit mode (old ``full_threshold``) is CANCELLED: the re-audit
    NEVER drags the full chapter (run_012 re-audit input was 41.5k tokens
    and truncated the 49k context); the region is chunked like the audit
    instead.
    """
    positions = {pid: i for i, pid in enumerate(all_pids)}
    scope: set = set()
    for pid in changed_pids:
        scope.add(pid)
        i = positions.get(pid)
        if i is None:
            continue
        for j in range(
            max(0, i - neighbour_window),
            min(len(all_pids), i + neighbour_window + 1),
        ):
            scope.add(all_pids[j])
    return tuple(pid for pid in all_pids if pid in scope)


# ---------------------------------------------------------------------------
# Review-candidate helpers (V4.2 R: Russian-editor REVIEW candidates)
# ---------------------------------------------------------------------------


def _next_index(findings: Sequence[EligibleFinding]) -> int:
    """Next free explicit index after ``findings`` (batch-local numbering)."""
    return max((f.index for f in findings), default=0)


def _review_candidate_finding(
    candidate: ReviewCandidate, *, index: int
) -> EligibleFinding:
    """Wrap one Russian-editor REVIEW candidate as a verify-before-repair
    finding (tier ``B`` — the verifier independently decides accept/reject
    against the ORIGINAL).

    ``excerpt`` carries the PROPOSED rewrite (what the verifier must judge);
    ``note`` carries the editor's reason. The ``issue`` dict carries a
    ``source: russian_editor`` marker plus the original/proposed/class so the
    accept/reject journal can be reconstructed after the batches.
    ``source_stage`` (CANDIDATE-MERGE, t_0ffe56e1) is ``candidate.source_stage``
    (``"russian_editor"``) — the repair prompt renders it so the verifier
    knows this remark is a Russian-defect hypothesis, not a source mismatch.
    """
    return EligibleFinding(
        index=index,
        pid=candidate.pid,
        tier="B",
        category=candidate.klass,
        severity="minor",
        confidence="high",
        note=candidate.reason,
        excerpt=candidate.proposed,
        issue={
            "id": candidate.pid,
            "category": candidate.klass,
            "severity": "minor",
            "confidence": "high",
            "note": candidate.reason,
            "excerpt": candidate.proposed,
            "source": "russian_editor",
            "original": candidate.original,
            "proposed": candidate.proposed,
            "class": candidate.klass,
        },
        source_stage=candidate.source_stage,
    )


def merge_candidates_by_pid(
    findings: Sequence[EligibleFinding],
) -> Tuple[EligibleFinding, ...]:
    """Group kept findings by PID before microbatching (CANDIDATE-MERGE,
    t_0ffe56e1; owner clarification 2026-08-13): ALL remarks of ONE pid —
    every fidelity-auditor finding AND every Russian-editor candidate,
    same-stage or cross-stage alike — merge into ONE ``EligibleFinding``
    that carries every remark in ``sources``, so the repair model sees the
    pid's complete remark set in ONE ``[index]`` block and builds ONE
    decision — never partial/sequential rewrites of the same paragraph
    (run_remote_001 p00303-class: the fidelity repair made an exact-but-
    clunky Russian, then the editor candidate rewrote it back with meaning
    loss; two blocks force partial decisions and let remarks get ignored).

    Rules (owner clarification 2026-08-13 — supersedes the t_78a3d02c
    partial-merge rule):

    * EVERY finding of one PID merges into a single finding — same-stage
      included. ``[A1, A2, E]`` -> ONE finding with ``sources=[A1, A2, E]``
      (three remarks, three stages where applicable), never
      ``[merged(A1+E), A2]``. The repair model must see ALL remarks of the
      pid at once to build one complete decision; keeping same-stage
      remarks in a second block makes it decide partially.
    * The merged finding keeps the FIRST finding's index (source order), its
      own ``source_stage`` joins every distinct stage with ``+`` and
      ``sources`` carries every remark (stage, tier, category, severity,
      confidence, note, excerpt, issue) so the prompt renderer can show them
      all (``_render_finding_block`` already renders N sources). All other
      indices of the group are consumed; indices re-number contiguously.
    * The merge runs BEFORE the cap re-numbering/ordering is undone: the
      findings passed in are the post-cap kept set (audit findings first,
      review candidates appended after), so an editor candidate never
      displaces a code-confirmed audit finding at the cap boundary.
    """
    by_pid: dict = {}
    for f in findings:
        by_pid.setdefault(f.pid, []).append(f)
    merged: list = []
    for f in findings:
        group = by_pid[f.pid]
        if group is None:
            continue
        merged.append(_merge_source_group(group))
        by_pid[f.pid] = None  # emitted once
    # Re-index contiguously in source order (indices must be unique — a
    # merged finding consumes its group's indices).
    out: list = []
    for i, f in enumerate(merged, start=1):
        out.append(
            EligibleFinding(
                index=i,
                pid=f.pid,
                tier=f.tier,
                category=f.category,
                severity=f.severity,
                confidence=f.confidence,
                note=f.note,
                excerpt=f.excerpt,
                issue=f.issue,
                source_stage=f.source_stage,
                sources=f.sources,
            )
        )
    return tuple(out)


def _merge_source_group(group: Sequence[EligibleFinding]) -> EligibleFinding:
    """Merge a same-PID group into ONE finding carrying all remarks in
    ``sources`` (CANDIDATE-MERGE, t_0ffe56e1; owner clarification
    2026-08-13: same-stage and cross-stage remarks alike). The first finding
    (source order — audit findings precede review candidates in the kept
    set) keeps its tier/severity/confidence/note/excerpt as the headline
    values; ``source_stage`` joins every distinct stage with ``+``."""
    primary = group[0]
    sources = tuple(
        {
            "stage": g.source_stage,
            "tier": g.tier,
            "category": g.category,
            "severity": g.severity,
            "confidence": g.confidence,
            "note": g.note,
            "excerpt": g.excerpt,
            "issue": dict(g.issue),
        }
        for g in group
    )
    stage_label = "+".join(sorted({g.source_stage for g in group}))
    return EligibleFinding(
        index=primary.index,
        pid=primary.pid,
        tier=primary.tier,
        category=primary.category,
        severity=primary.severity,
        confidence=primary.confidence,
        note=primary.note,
        excerpt=primary.excerpt,
        issue=primary.issue,
        source_stage=stage_label,
        sources=sources,
    )


def _finding_has_editor_source(finding: EligibleFinding) -> bool:
    """True when a finding carries the Russian-editor stage — either as a
    plain review candidate (``issue.source == \"russian_editor\"``) or as one
    remark of a CANDIDATE-MERGE group (``sources[].stage == \"russian_editor\"``,
    t_0ffe56e1). The merged finding's headline ``issue`` is the audit issue,
    so the source marker must be searched in ``sources`` too."""
    if any(s.get("stage") == "russian_editor" for s in finding.sources):
        return True
    return finding.issue.get("source") == "russian_editor"


def _build_review_journal(
    batch_outcomes: Sequence[RepairBatchOutcome],
    review_candidates: Sequence[ReviewCandidate],
) -> Tuple[Dict[str, Any], ...]:
    """Reconstruct the accept/reject journal for the review candidates.

    One entry per candidate: ``{pid, class, original, proposed, verdict,
    reason, committed_text}`` where verdict is ``accepted`` (the verifier
    returned ``repair`` — the accepted text is committed and re-audited),
    ``rejected`` (verifier returned ``pass``), or ``failed`` (the batch
    failed / the index was never answered — fail-closed, never silently
    accepted). With CANDIDATE-MERGE (t_0ffe56e1, owner clarification
    2026-08-13) a candidate is always served by the PID's SINGLE merged
    finding (ALL remarks of the pid live in one index) — the journal verdict
    comes from that one index's answer (one decision for the whole PID; a
    pid appears at most once per batch, so there is no sub-index ambiguity).
    """
    journal: list = []
    for candidate in review_candidates:
        entry: Dict[str, Any] = {
            "pid": candidate.pid,
            "class": candidate.klass,
            "original": candidate.original,
            "proposed": candidate.proposed,
            "verdict": "failed",
            "reason": "review candidate was never answered",
            "committed_text": None,
        }
        for batch in batch_outcomes:
            finding = next(
                (
                    f for f in batch.findings
                    if f.pid == candidate.pid and _finding_has_editor_source(f)
                ),
                None,
            )
            if finding is None:
                continue
            if batch.status == "FAILED":
                entry["verdict"] = "failed"
                entry["reason"] = f"failed repair batch: {batch.error}"
                break
            result = next(
                (r for r in batch.results if r.index == finding.index), None
            )
            if result is None:
                entry["verdict"] = "failed"
                entry["reason"] = (
                    "review candidate index was not answered in the batch"
                )
                break
            if result.decision == "repair":
                entry["verdict"] = "accepted"
                entry["reason"] = result.reason
                entry["committed_text"] = result.repaired_translation
            else:
                entry["verdict"] = "rejected"
                entry["reason"] = result.reason
            break
        journal.append(entry)
    return tuple(journal)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _repair_batches_payload(
    batch_outcomes: Sequence[RepairBatchOutcome],
) -> List[Dict[str, Any]]:
    """Per-batch payload slices for the KILL-SAFE-INCREMENTAL progress hook.

    Mirrors ``SelectiveRepairOutcome.to_payload()["batches"]`` exactly (same
    per-batch key set), so the incremental cache validator checks the same
    schema as the final repair payload. A cached GOOD batch is replayed on
    resume only when its ``findings`` pids match the current batch.
    """
    return [
        {
            "batch_index": b.batch_index,
            "status": b.status,
            "findings": [
                {
                    "index": f.index,
                    "pid": f.pid,
                    "tier": f.tier,
                    "category": f.category,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "source_stage": f.source_stage,
                    "sources": [dict(s) for s in f.sources],
                }
                for f in b.findings
            ],
            "results": [
                {
                    "index": r.index,
                    "decision": r.decision,
                    "pid": r.pid,
                    "repaired_translation": r.repaired_translation,
                    "reason": r.reason,
                }
                for r in b.results
            ],
            "error": b.error,
            "warnings": list(b.warnings),
            "missing_indices": list(b.missing_indices),
        }
        for b in batch_outcomes
    ]


class SelectiveRepairEvaluator:
    """B2 selective repair + repair-as-verifier over ``CompletionBackend``.

    Usage::

        evaluator = SelectiveRepairEvaluator(repair_backend, config=config)
        outcome = evaluator(
            chapter_id="0001",
            source=source_map,
            translation=translation_map,
            filtered=apply_hard_filters(issues, source=..., translation=...),
            entity_context="...",  # chapter entity facts (Tier B context)
        )

    ``repair_backend`` serves the GENERATOR role (Gemma local / DeepSeek
    remote — Kocmi-safe); ``reaudit_backend`` (optional, defaults to the same
    object) serves the Qwen re-audit. The ``on_phase`` callback (optional) is
    invoked with ``"repair"`` before the first repair call and ``"reaudit"``
    before the re-audit call — the lifecycle wrapper uses it to call
    ``ModelRouter.ensure_resident`` per phase.
    """

    def __init__(
        self,
        repair_backend: CompletionBackend,
        *,
        reaudit_backend: Optional[CompletionBackend] = None,
        config: Optional[SelectiveRepairConfig] = None,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._repair_backend = repair_backend
        self._reaudit_backend = reaudit_backend or repair_backend
        self._config = config or SelectiveRepairConfig()
        # KILL-SAFE-INCREMENTAL (t_2d16962c): accumulated-state hook fired
        # after EVERY batch and EVERY reaudit chunk with the partial repair /
        # reaudit slices built so far. The B3 orchestrator uses it to rewrite
        # audit_cache_b3.json incrementally (stage_progress.repair /
        # .reaudit), so a kill preserves every completed batch / reaudit
        # chunk.
        self._on_progress = on_progress

    def _emit_progress(self, kind: str, **fields: Any) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(kind, fields)
            except Exception:  # noqa: BLE001 — a progress hook never breaks repair
                LOG.debug("selective_repair on_progress(%r) failed", kind, exc_info=True)

    @property
    def repair_backend(self) -> CompletionBackend:
        return self._repair_backend

    def __call__(
        self,
        *,
        chapter_id: str,
        source: Mapping[str, str],
        translation: Mapping[str, str],
        filtered: Sequence[FilteredIssue],
        entity_context: str = "",
        narrator_context: str = "",
        review_candidates: Sequence[ReviewCandidate] = (),
        on_phase: Optional[Callable[[str], None]] = None,
        out_dir: Optional[Path] = None,
        out_base: str = "b3_repair",
        # KILL-SAFE-INCREMENTAL (t_2d16962c): per-batch repair resume plan
        # ``{batch_index: {status, findings_pids, results}}`` — a cached GOOD
        # batch whose finding pids match the CURRENT batch is replayed with 0
        # model calls (committed/passed reused verbatim); a mismatch is a
        # fail-closed re-run.
        cached_batches: Optional[Mapping[int, Mapping[str, Any]]] = None,
        # Reaudit chunk resume plan ``{chunk_index: {first_pid, last_pid,
        # issues}}`` — a cached chunk whose boundaries match the current
        # chunk is replayed with 0 model calls.
        cached_reaudit_chunks: Optional[Mapping[int, Mapping[str, Any]]] = None,
    ) -> SelectiveRepairOutcome:
        cfg = self._config
        model_ref = repair_model_ref(self._repair_backend)
        phase = on_phase or (lambda _name: None)

        eligible, rejected, ineligible = select_eligible(
            filtered, allowed_categories=cfg.allowed_categories
        )
        kept, capped = apply_findings_cap(eligible, cap=cfg.findings_cap)
        debt: list = []
        for f in capped:
            debt.append(
                f"pid {f.pid}: {POLICY_LIMIT_TAG} (eligible but beyond the "
                f"{cfg.findings_cap}-finding cap)"
            )
        for f in ineligible:
            debt.append(
                f"pid {f.pid}: not eligible for repair "
                f"(confidence={f.confidence}, category={f.category}) — debt/diagnostic"
            )

        # V4.2 R (card t_4707e6e5): REVIEW-classed Russian-editor candidates
        # are ADDITIONAL verify-before-repair input (the verifier accepts or
        # rejects each against the ORIGINAL; accepted -> commit + re-audit).
        # They are appended AFTER the audit findings cap so they never
        # displace code-confirmed audit findings at the cap boundary, and
        # their accept/reject verdicts are journaled for the trial record.
        # Indices continue after the kept findings (1-based, unique — the
        # model answers per [index]).
        kept = tuple(kept) + tuple(
            _review_candidate_finding(c, index=_next_index(kept) + i + 1)
            for i, c in enumerate(review_candidates)
        )

        # CANDIDATE-MERGE (t_0ffe56e1; owner clarification 2026-08-13):
        # group the kept findings by PID BEFORE microbatching — ALL remarks
        # of ONE pid (every fidelity-auditor finding AND every Russian-editor
        # candidate, same-stage or cross-stage alike) become ONE
        # EligibleFinding (source_stage joins the distinct stages, every
        # remark in ``sources``) so the repair model sees the pid's complete
        # remark set in a single call and builds ONE decision. run_remote_001
        # p00303-class: without the merge the fidelity repair first wrote an
        # exact-but-clunky Russian, then the editor candidate rewrote the
        # same PID back losing the source meaning. The merge runs on the
        # post-cap kept set (audit first, candidates appended) — it never
        # re-opens the cap.
        kept = merge_candidates_by_pid(kept)

        if not kept:
            # TEaR: 0 eligible findings -> repair skipped entirely.
            return SelectiveRepairOutcome(
                schema=REPAIR_SCHEMA,
                harness_version=cfg.harness_version,
                prompt_version=cfg.prompt_version,
                model=model_ref,
                eligible_count=len(eligible),
                capped=capped,
                rejected=rejected,
                ineligible=ineligible,
                batches=(),
                committed=(),
                passed_pids=(),
                debt_trace=tuple(debt),
                reaudit=None,
                repair_complete=True,
                skipped=True,
                review_journal=(),
            )

        batches = make_microbatches(
            kept,
            trigger=cfg.microbatch_trigger,
            target=cfg.microbatch_target,
        )
        phase("repair")
        batch_outcomes: list = []
        committed: dict = {}
        passed_pids: list = []
        for batch_index, batch in enumerate(batches, start=1):
            # KILL-SAFE-INCREMENTAL (t_2d16962c): a cached GOOD batch whose
            # finding pids EXACTLY match the current batch is replayed with 0
            # model calls — its stored results (committed/passed) are reused
            # verbatim. Any mismatch (changed findings after a partial audit
            # replay) is a fail-closed re-run, never a partial replay.
            cached = (cached_batches or {}).get(batch_index)
            if (
                cached is not None
                and cached.get("status") == "GOOD"
                and list(cached.get("findings_pids") or ())
                == [f.pid for f in batch]
            ):
                cached_results = [
                    RepairResult(
                        index=int(r.get("index", 0)),
                        decision=str(r.get("decision", "")),
                        pid=str(r.get("pid", "")),
                        repaired_translation=str(r.get("repaired_translation", "")),
                        reason=str(r.get("reason", "")),
                    )
                    for r in cached.get("results") or ()
                ]
                LOG.info(
                    "B2: replaying cached GOOD repair batch %d (%d finding(s)) "
                    "with 0 model calls (KILL-SAFE-INCREMENTAL)",
                    batch_index, len(batch),
                )
                outcome = RepairBatchOutcome(
                    batch_index=batch_index,
                    status="GOOD",
                    findings=tuple(batch),
                    results=tuple(cached_results),
                )
            else:
                outcome = self._run_batch(
                    chapter_id=chapter_id,
                    source=source,
                    translation=translation,
                    findings=batch,
                    batch_index=batch_index,
                    out_dir=out_dir,
                    out_base=out_base,
                )
            batch_outcomes.append(outcome)
            finding_by_index = {f.index: f for f in batch}
            if outcome.status == "FAILED":
                debt.append(
                    f"failed repair batch {batch_index}: {outcome.error} "
                    f"(findings {[f.pid for f in outcome.findings]})"
                )
                self._emit_progress(
                    "batch_done",
                    batches=_repair_batches_payload(batch_outcomes),
                    committed=sorted(committed.items()),
                    passed_pids=list(passed_pids),
                    batch_count=len(batches),
                )
                continue
            for result in outcome.results:
                if result.decision == "repair":
                    committed[result.pid] = result.repaired_translation
                else:
                    # The model answers PASS by index without echoing the PID;
                    # resolve it from the batch finding (the index contract).
                    passed_pids.append(finding_by_index[result.index].pid)
            # REPAIR-ROBUST-PARTIAL (t_c0cb8e3c): a tolerant response that
            # recovered SOME but not ALL of the batch's findings keeps its
            # valid recovered repairs (committed above), but every missing
            # finding is routed to debt — a partial response is NEVER
            # published as complete (repair_complete stays False via the
            # PARTIAL batch status).
            if outcome.status == "PARTIAL":
                for index in outcome.missing_indices:
                    finding = finding_by_index.get(index)
                    pid = finding.pid if finding is not None else f"index {index}"
                    debt.append(
                        f"pid {pid}: no repair answer recovered for finding "
                        f"index {index} (partial tolerant repair — recovered "
                        f"{len(outcome.results)}/{len(outcome.findings)})"
                    )
            self._emit_progress(
                "batch_done",
                batches=_repair_batches_payload(batch_outcomes),
                committed=sorted(committed.items()),
                passed_pids=list(passed_pids),
                batch_count=len(batches),
            )

        review_journal = _build_review_journal(batch_outcomes, review_candidates)
        # REPAIR-2 (t_768537b9): aggregate per-index non-fatal notices from
        # the batches (no-op repairs converted to pass) for the journal.
        batch_warnings = tuple(
            warning for outcome in batch_outcomes for warning in outcome.warnings
        )

        reaudit: Optional[ReauditOutcome] = None
        repair_complete = all(b.status == "GOOD" for b in batch_outcomes)
        if committed and cfg.reaudit_enabled:
            phase("reaudit")
            reaudit = self._run_reaudit(
                chapter_id=chapter_id,
                source=source,
                translation=dict(translation, **committed),
                original_translation=translation,
                changed_pids=tuple(committed),
                entity_context=entity_context,
                narrator_context=narrator_context,
                out_dir=out_dir,
                out_base=out_base,
                # KILL-SAFE-INCREMENTAL (t_2d16962c): replay GOOD reaudit
                # chunks from the partial cache (0 model calls); the rest are
                # re-run. A boundary mismatch re-runs the chunk (fail-closed).
                cached_chunks=cached_reaudit_chunks,
            )
            if reaudit.failed:
                repair_complete = False
                debt.append(f"failed re-audit: {reaudit.reason}")
            elif reaudit.issues:
                debt.append(
                    f"re-audit found {len(reaudit.issues)} residual finding(s) "
                    f"in scope {list(reaudit.scope)}"
                )

        return SelectiveRepairOutcome(
            schema=REPAIR_SCHEMA,
            harness_version=cfg.harness_version,
            prompt_version=cfg.prompt_version,
            model=model_ref,
            eligible_count=len(eligible),
            capped=capped,
            rejected=rejected,
            ineligible=ineligible,
            batches=tuple(batch_outcomes),
            committed=tuple(sorted(committed.items())),
            passed_pids=tuple(passed_pids),
            debt_trace=tuple(debt),
            reaudit=reaudit,
            repair_complete=repair_complete,
            skipped=False,
            warnings=batch_warnings,
            review_journal=review_journal,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _write_batch_artifacts(
        *,
        out_dir: Optional[Path],
        out_base: str,
        batch_index: int,
        content: str,
        reasoning: str,
    ) -> None:
        """Persist one repair batch's raw response + reasoning.

        Mirrors ``ChunkedAudit._write_artifacts``: ``b3_repair_batch{N}_raw.txt``
        / ``b3_repair_batch{N}_reasoning.txt``. Written on EVERY batch — a
        parse failure (incl. the 'truncated repair' gate) then leaves a disk
        trail (run_011 lesson: truncated PIDs were undiagnosable).
        """
        if out_dir is None:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{out_base}_batch{batch_index}_raw.txt").write_text(
            content, encoding="utf-8"
        )
        (out_dir / f"{out_base}_batch{batch_index}_reasoning.txt").write_text(
            reasoning, encoding="utf-8"
        )

    def _run_batch(
        self,
        *,
        chapter_id: str,
        source: Mapping[str, str],
        translation: Mapping[str, str],
        findings: Sequence[EligibleFinding],
        batch_index: int,
        out_dir: Optional[Path] = None,
        out_base: str = "b3_repair",
    ) -> RepairBatchOutcome:
        cfg = self._config
        prompt = render_selective_repair_prompt(
            chapter_id=chapter_id,
            source=dict(source),
            translation=dict(translation),
            findings=findings,
            template=cfg.template,
            repair_context_window=cfg.repair_context_window,
            repair_context_window_by_category=cfg.repair_context_window_by_category,
        )
        model_ref = repair_model_ref(self._repair_backend)
        # REASONING-STREAM: the reasoning file is created BEFORE the call and
        # grows live via on_reasoning_chunk (gemma_rewrite_v4 pattern); the
        # authoritative write after completion stays unchanged.
        reason_path: Optional[Path] = None
        if out_dir is not None:
            reason_path = out_dir / f"{out_base}_batch{batch_index}_reasoning.txt"
        # REPAIR-ROBUST (card t_b6fd6cbd, run_0005): the repair reasoning
        # effort (default 1 = low) is transported via request_options ONLY
        # to remote-capable backends (opencode maps it to reasoningEffort).
        # Local llama-server transports receive the budget from their server
        # args (--reasoning-budget) and LocalOpenAIBackend rejects any
        # request_options (library guard) — the local path is untouched
        # (owner rule: local servers always run with the same args).
        request_options: Dict[str, Any] = {}
        if cfg.repair_reasoning and _reasoning_transported_via_request_options(
            self._repair_backend, model_ref
        ):
            request_options["reasoning"] = cfg.repair_reasoning
        request = CompletionRequest(
            model_ref=model_ref,
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=cfg.max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=cfg.label,
            on_reasoning_chunk=open_reasoning_writer(reason_path),
            request_options=request_options,
        )
        try:
            response = self._repair_backend.complete(request)
        except Exception as exc:  # CompletionError and any transport-level failure
            LOG.error("repair batch transport failure (%s): %s", type(exc).__name__, exc)
            # Raw error trail + preserve any reasoning that streamed live
            # before the failure instead of wiping it.
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{out_base}_batch{batch_index}_raw.txt").write_text(
                    f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
                append_error_marker(reason_path, exc)
            return RepairBatchOutcome(
                batch_index=batch_index,
                status="FAILED",
                findings=tuple(findings),
                error=f"{type(exc).__name__}: {exc}",
            )
        content = response.text or ""
        reasoning = str((response.raw_metadata or {}).get("reasoning") or "")
        # B1 (run_011): persist raw + reasoning on EVERY batch — a parse
        # failure (incl. a 'truncated repair' gate hit) leaves a disk trail.
        self._write_batch_artifacts(
            out_dir=out_dir, out_base=out_base, batch_index=batch_index,
            content=content, reasoning=reasoning,
        )
        results, errors, warnings = parse_repair_batch(
            content, findings, current_by_pid=dict(translation)
        )
        if warnings:
            LOG.warning(
                "repair batch %s: %d non-fatal warning(s): %s",
                batch_index, len(warnings), "; ".join(warnings),
            )
        if errors:
            return RepairBatchOutcome(
                batch_index=batch_index,
                status="FAILED",
                findings=tuple(findings),
                results=results,
                error="; ".join(errors),
                warnings=warnings,
            )
        # REPAIR-ROBUST-PARTIAL (t_c0cb8e3c): a response that did NOT answer
        # every finding (the tolerant salvage recovered some but not all of
        # the batch) is PARTIAL — the recovered repairs are retained, but the
        # missing indices are surfaced in the outcome and routed to debt by
        # the evaluator, so ``repair_complete`` can never become True for a
        # partial response. A warning alone is not enough: the 50% salvage
        # threshold must never act as a publication-complete threshold.
        answered = {r.index for r in results}
        expected = {f.index for f in findings}
        missing = sorted(expected - answered)
        if missing:
            return RepairBatchOutcome(
                batch_index=batch_index,
                status="PARTIAL",
                findings=tuple(findings),
                results=results,
                warnings=warnings,
                missing_indices=tuple(missing),
            )
        return RepairBatchOutcome(
            batch_index=batch_index,
            status="GOOD",
            findings=tuple(findings),
            results=results,
            warnings=warnings,
        )

    @staticmethod
    def _write_reaudit_artifacts(
        *,
        out_dir: Optional[Path],
        out_base: str,
        chunk_index: int,
        content: str,
        reasoning: str,
    ) -> None:
        """Persist one re-audit chunk's raw response + reasoning.

        ``b3_repair_reaudit_chunk{N}_raw.txt`` /
        ``b3_repair_reaudit_chunk{N}_reasoning.txt`` (REPAIR-CTX: the re-audit
        is chunked like the audit — one artifact pair per chunk). Written on
        EVERY attempt, incl. transport failure (run_010 lesson: an empty JSON
        of 8265 tokens with content=0 was undiagnosable without the raw
        trail).
        """
        if out_dir is None:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{out_base}_reaudit_chunk{chunk_index}_raw.txt").write_text(
            content, encoding="utf-8"
        )
        (out_dir / f"{out_base}_reaudit_chunk{chunk_index}_reasoning.txt").write_text(
            reasoning, encoding="utf-8"
        )

    def _run_reaudit(
        self,
        *,
        chapter_id: str,
        source: Mapping[str, str],
        translation: Mapping[str, str],
        original_translation: Mapping[str, str],
        changed_pids: Sequence[str],
        entity_context: str,
        narrator_context: str = "",
        out_dir: Optional[Path] = None,
        out_base: str = "b3_repair",
        # KILL-SAFE-INCREMENTAL (t_2d16962c): per-chunk reaudit resume plan
        # ``{chunk_index: {first_pid, last_pid, issues}}`` — a cached chunk
        # whose boundaries match the CURRENT chunk is replayed with 0 model
        # calls; a boundary mismatch re-runs the chunk (fail-closed).
        cached_chunks: Optional[Mapping[int, Mapping[str, Any]]] = None,
    ) -> ReauditOutcome:
        cfg = self._config
        try:
            pairs = pairs_from_maps(source, translation)
        except Exception as exc:
            return ReauditOutcome(
                complete=False, failed=True, reason=f"pair construction failed: {exc}"
            )
        all_pids = [p.pid for p in pairs]
        scope_pids = plan_reaudit_scope(
            changed_pids,
            all_pids,
            neighbour_window=cfg.reaudit_neighbour_window,
        )
        # Fail-loud (REPAIR-CTX): every re-audited PID must be present in the
        # maps — the model cannot re-audit a PID it cannot see.
        missing = [pid for pid in changed_pids if pid not in all_pids]
        if missing:
            return ReauditOutcome(
                complete=False, failed=True, scope=scope_pids,
                reason=f"re-audit changed PID(s) {missing} missing from "
                       f"source/translation maps — the model cannot re-audit "
                       f"what it cannot see",
            )
        by_pid = {p.pid: p for p in pairs}
        scope_pairs = tuple(by_pid[pid] for pid in scope_pids if pid in by_pid)
        # REPAIR-CTX (owner decision 2026-08-12): the re-audit is a CHUNKED
        # audit over the affected region, reusing the audit's chunking /
        # overlap mechanisms (build_greedy_chunks / get_overlap_context) —
        # NEVER the whole chapter (run_012 re-audit input was 41.5k tokens
        # and truncated the 49k context).
        chunks = build_greedy_chunks(
            scope_pairs, max_input=cfg.reaudit_max_input_tokens,
        )
        repaired_changes = tuple(
            RepairedChange(
                pid=pid,
                before=str(original_translation.get(pid, "")),
                after=str(translation.get(pid, "")),
            )
            for pid in changed_pids
        )
        model_ref = audit_model_ref(self._reaudit_backend)
        all_issues: List[Mapping[str, Any]] = []
        errors: List[str] = []
        chunk_records: List[Dict[str, Any]] = []
        for chunk_index, chunk_pairs in enumerate(chunks, start=1):
            chunk_pids = [p.pid for p in chunk_pairs]
            # KILL-SAFE-INCREMENTAL (t_2d16962c): a cached reaudit chunk whose
            # boundaries match the CURRENT chunk is replayed with 0 model
            # calls (its stored residual issues reused verbatim). A boundary
            # mismatch (changed repair scope after a partial replay) is a
            # fail-closed re-run, never a partial replay.
            cached = (cached_chunks or {}).get(chunk_index)
            if (
                cached is not None
                and str(cached.get("first_pid")) == chunk_pids[0]
                and str(cached.get("last_pid")) == chunk_pids[-1]
                # CONTEXT-PID-DROP (RV5 t_f82ed9ad): a cached chunk that
                # FAILED fresh (malformed dropped diagnostics etc.) is NEVER
                # replayed — even if its boundaries match — so the malformed
                # input's diagnostic/debt is preserved instead of a 0-call
                # replay silently upgrading the chunk to complete.
                and cached.get("failed") is not True
            ):
                replayed_issues = [
                    dict(item) for item in (cached.get("issues") or ())
                ]
                LOG.info(
                    "B2: replaying cached GOOD reaudit chunk %d (%d issue(s)) "
                    "with 0 model calls (KILL-SAFE-INCREMENTAL)",
                    chunk_index, len(replayed_issues),
                )
                all_issues.extend(replayed_issues)
                chunk_records.append({
                    "chunk": chunk_index,
                    "first_pid": chunk_pids[0],
                    "last_pid": chunk_pids[-1],
                    "issues": replayed_issues,
                    # CONTEXT-PID-DROP: the dropped context/foreign issue
                    # objects ride the cached replay too, so a killed/resumed
                    # re-audit keeps the journaled diagnostics of the fresh
                    # run (fail-closed validation happened at cache load).
                    "dropped": [
                        dict(item) for item in (cached.get("dropped") or ())
                        if isinstance(item, dict)
                    ],
                    # CONTEXT-PID-DROP (RV5 t_f82ed9ad): a replayed cached
                    # chunk is by construction NOT failed (the replay guard
                    # above refuses failed records, and reaudit_resume_plan
                    # excludes them) — the marker rides the done record so
                    # the persisted stage_progress round-trips cleanly.
                    "failed": False,
                })
                self._emit_progress(
                    "reaudit_chunk_done",
                    done_chunks=list(chunk_records),
                    chunk_count=len(chunks),
                )
                continue
            context_pairs = get_overlap_context(
                pairs,
                chunk_pids[0],
                cfg.reaudit_overlap_tokens,
                cfg.reaudit_min_overlap_pairs,
                cfg.reaudit_max_overlap_pairs,
            )
            prompt = render_reaudit_prompt(
                chapter_id=chapter_id,
                audit_pairs=chunk_pairs,
                context_pairs=context_pairs,
                repaired_changes=repaired_changes,
                narrator_context=narrator_context,
                entity_context=entity_context,
                chunk_index=chunk_index,
                chunk_total=len(chunks),
            )
            # REASONING-STREAM: the reasoning file is created BEFORE the call
            # and grows live via on_reasoning_chunk (gemma_rewrite_v4 pattern);
            # the per-attempt authoritative write below stays unchanged.
            reason_path: Optional[Path] = None
            if out_dir is not None:
                reason_path = out_dir / (
                    f"{out_base}_reaudit_chunk{chunk_index}_reasoning.txt"
                )
            request = CompletionRequest(
                model_ref=model_ref,
                messages=(Message(role="user", content=prompt),),
                max_output_tokens=cfg.reaudit_max_tokens,
                temperature=0.0,
                response_schema=JSON_OBJECT_SCHEMA,
                label=cfg.reaudit_label,
                on_reasoning_chunk=open_reasoning_writer(reason_path),
            )

            def _complete() -> str:
                # Re-issues the IDENTICAL request on a retry (same prompt,
                # same max_output_tokens, same model_ref, same backend — B4
                # §4), so a retry never changes cache/resume identity. A
                # transport failure (CompletionError or any subclass)
                # propagates immediately and is never retried as a JSON
                # problem (B4 §1/§3).
                try:
                    response = self._reaudit_backend.complete(request)
                except CompletionError as exc:
                    LOG.error(
                        "re-audit chunk %s transport failure (%s): %s",
                        chunk_index, type(exc).__name__, exc,
                    )
                    # C1 (run_010): even a transport failure leaves a disk
                    # trail — the empty-JSON mystery (8265 tokens, content=0)
                    # must be diagnosable from the artifact, not just the
                    # journal. Preserve any reasoning streamed live before
                    # the failure instead of wiping it.
                    if out_dir is not None:
                        out_dir.mkdir(parents=True, exist_ok=True)
                        (out_dir / (
                            f"{out_base}_reaudit_chunk{chunk_index}_raw.txt"
                        )).write_text(
                            f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}\n",
                            encoding="utf-8",
                        )
                        append_error_marker(reason_path, exc)
                    raise
                text = response.text or ""
                reasoning = str((response.raw_metadata or {}).get("reasoning") or "")
                # C1 (run_010): persist raw + reasoning on EVERY attempt — a
                # final empty/truncated body still leaves the reasoning that
                # caused it.
                self._write_reaudit_artifacts(
                    out_dir=out_dir, out_base=out_base,
                    chunk_index=chunk_index,
                    content=text, reasoning=reasoning,
                )
                return text

            try:
                content = retry_json_call(
                    _complete, cfg.reaudit_retry, label=cfg.reaudit_label,
                )
            except (EmptyResponseError, TruncatedJSONError) as exc:
                # Budget exhausted: the last attempt still returned an
                # empty/truncated body. Fail-closed debt — never "0 findings".
                return ReauditOutcome(
                    complete=False,
                    failed=True,
                    scope=scope_pids,
                    reason=f"re-audit chunk {chunk_index} response invalid after "
                           f"{cfg.reaudit_retry.max_retries + 1} attempt(s): {exc}",
                )
            except CompletionError as exc:
                return ReauditOutcome(
                    complete=False,
                    failed=True,
                    scope=scope_pids,
                    reason=f"re-audit chunk {chunk_index}: {type(exc).__name__}: {exc}",
                )
            except Exception as exc:  # noqa: BLE001 — any transport-level failure is debt, never a crash
                LOG.error(
                    "re-audit chunk %s unexpected failure (%s): %s",
                    chunk_index, type(exc).__name__, exc,
                )
                return ReauditOutcome(
                    complete=False,
                    failed=True,
                    scope=scope_pids,
                    reason=f"re-audit chunk {chunk_index}: {type(exc).__name__}: {exc}",
                )
            try:
                parsed = parse_json_response(content)
            except Exception as exc:
                return ReauditOutcome(
                    complete=False,
                    failed=True,
                    scope=scope_pids,
                    reason=f"re-audit chunk {chunk_index} response is not valid JSON: {exc}",
                )
            # CONTEXT-PID-DROP (owner 2026-08-15): the re-audit model is
            # given context_pairs for continuity and must NOT re-audit them —
            # an issue on a context pid is dropped per-issue (journaled in
            # the chunk record for diagnostics), never a chunk failure, so
            # re-audit no longer falls into failed=True debt (run gl.6
            # p00251 case).
            context_pids = [p.pid for p in context_pairs]
            validation = validate_chunk_json(parsed, chunk_pids, context_pids=context_pids)
            # CONTEXT-PID-DROP (RV5 t_f82ed9ad): per-chunk failure tracking
            # for the done record's ``failed`` marker — a chunk that surfaces
            # ANY error (invalid chunk JSON or a malformed dropped issue)
            # is marked failed and is NEVER replayable on resume.
            chunk_failed = False
            if not validation.valid:
                errors.append(
                    f"chunk {chunk_index}: " + "; ".join(validation.errors)
                )
                chunk_failed = True
            # Dropped issues (context-only/foreign pids) are journaled but
            # NEVER extend the re-audit findings — they are not in scope.
            all_issues.extend(validation.issues)
            # CONTEXT-PID-DROP (RV2 t_61af1bb2): dropped issues are journaled
            # as COMPLETE well-formed issue objects — with the same harness
            # ``_debug`` metadata the audit attaches to its cached issues
            # ({chunk, reasoning_file}; the reasoning file name matches
            # _write_reaudit_artifacts), so a persisted dropped record
            # satisfies the exact _ISSUE_KEYS contract the incremental cache
            # validator enforces on resume (malformed dropped objects are a
            # full miss, never a trusted replay that loses diagnostics).
            # CONTEXT-PID-DROP (RV4 t_cfb1523d): a fresh scope-dropped issue
            # must be a structurally well-formed canonical issue object
            # BEFORE it is journaled — validate_chunk_json checked only
            # id/category/severity/confidence pre-drop, so a dropped issue
            # with a missing/non-string note/excerpt (or any other
            # non-canonical value) would otherwise be persisted as a
            # malformed dropped object and full-miss the WHOLE
            # stage-progress cache on resume (reload fails closed, the
            # dropped diagnostic lost). Malformed dropped inputs fail the
            # chunk closed (debt, the issue named in the outcome reason)
            # and are NEVER persisted; only contract-canonical dropped
            # issues are journaled.
            journaled_dropped: List[Dict[str, Any]] = []
            for dropped_issue in validation.dropped:
                dropped_error = _reaudit_dropped_issue_error(dropped_issue)
                if dropped_error is not None:
                    errors.append(
                        f"chunk {chunk_index}: dropped issue "
                        f"{dropped_issue.get('id')!r}: {dropped_error}"
                    )
                    chunk_failed = True
                    continue
                journaled_dropped.append({
                    # CONTEXT-PID-DROP (RV3 t_c9eb65d4): retain ONLY the
                    # canonical issue fields before attaching the harness
                    # _debug. validate_chunk_json accepts a well-formed
                    # context/foreign issue carrying an unknown EXTRA
                    # model field — journaling it verbatim would emit a
                    # dropped object with keys outside the exact
                    # _ISSUE_KEYS contract, which _validate_stage_progress
                    # rejects fail-closed on reload (full miss = the
                    # dropped diagnostic is lost). Canonical-only keeps
                    # the persisted object exact-schema.
                    **{k: dropped_issue[k] for k in _REAUDIT_DROPPED_FIELDS if k in dropped_issue},
                    "_debug": {
                        "chunk": chunk_index,
                        "reasoning_file": (
                            f"{out_base}_reaudit_chunk{chunk_index}_reasoning.txt"
                        ),
                    },
                })
            chunk_records.append({
                "chunk": chunk_index,
                "first_pid": chunk_pids[0],
                "last_pid": chunk_pids[-1],
                "issues": [dict(i) for i in validation.issues],
                "dropped": journaled_dropped,
                # CONTEXT-PID-DROP (RV5 t_f82ed9ad): the failed marker makes
                # this done record explicit — a chunk with errors (invalid
                # chunk JSON or malformed dropped diagnostics) is NEVER a
                # replayable-complete record. reaudit_resume_plan excludes
                # failed records, so the next resume re-runs the chunk
                # (fail-closed debt/diagnostic preserved) instead of
                # replaying it with 0 model calls and silently upgrading it
                # to complete.
                "failed": chunk_failed,
            })
            self._emit_progress(
                "reaudit_chunk_done",
                done_chunks=list(chunk_records),
                chunk_count=len(chunks),
            )
        if errors:
            return ReauditOutcome(
                complete=False,
                failed=True,
                scope=scope_pids,
                issues=tuple(all_issues),
                reason="; ".join(errors),
            )
        return ReauditOutcome(
            complete=True,
            failed=False,
            issues=tuple(all_issues),
            scope=scope_pids,
        )


__all__ = [
    "REPAIR_SCHEMA",
    "REPAIR_HARNESS_VERSION",
    "REPAIR_PROMPT_VERSION",
    "REPAIR_FINDINGS_CAP",
    "POLICY_LIMIT_TAG",
    "DEFAULT_REAUDIT_MAX_TOKENS",
    "DEFAULT_REAUDIT_MAX_RETRIES",
    "DEFAULT_REAUDIT_BASE_DELAY_SECONDS",
    "DEFAULT_REAUDIT_NEIGHBOUR_WINDOW",
    "DEFAULT_REPAIR_CONTEXT_WINDOW",
    "DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY",
    "REAUDIT_DELTA_FORMAT",
    "MICROBATCH_TRIGGER",
    "MICROBATCH_TARGET",
    "SelectiveRepairConfig",
    "EligibleFinding",
    "RepairResult",
    "RepairBatchOutcome",
    "ReauditOutcome",
    "RepairedChange",
    "SelectiveRepairOutcome",
    "repair_model_ref",
    "select_eligible",
    "apply_findings_cap",
    "make_microbatches",
    "parse_repair_batch",
    "plan_reaudit_scope",
    "merge_candidates_by_pid",
    "SelectiveRepairEvaluator",
    "ReviewCandidate",
]
