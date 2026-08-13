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
  Tier B findings are eligible ONLY at ``confidence=high`` AND within the
  allowed semantic categories (``changed_fact``/numeric claims are Tier A
  code-verified by B1.1, never guessed here). ``REJECTED`` findings are
  deterministic FPs — never repaired. Tier B below the eligibility bar goes
  to debt/diagnostic, never auto-repair.
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
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import (
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
from pact_v4.runtime.json_resilience import (
    EmptyResponseError,
    JsonRetryPolicy,
    TruncatedJSONError,
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

# Repair output budget (per batch call) and re-audit output budget. REPAIR-CTX
# (t_97b31f81): the re-audit input is now the affected REGION (changed PIDs +
# neighbours, chunked), not the full chapter — but the 20000-token output
# budget is kept (chunked JSON responses + reasoning headroom; the old
# 12000-token budget was exhausted by reasoning on the full input in
# run_010-style chapters — owner decision 2026-08-11).
DEFAULT_REPAIR_MAX_TOKENS = 4000
DEFAULT_REAUDIT_MAX_TOKENS = 20000

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
    status: str  # "GOOD" | "FAILED"
    findings: Tuple[EligibleFinding, ...] = ()
    results: Tuple[RepairResult, ...] = ()
    error: str = ""
    # REPAIR-2 (t_768537b9): per-index NON-FATAL notices journaled with the
    # batch — e.g. no-op repairs converted to per-index pass (the batch stays
    # GOOD when they are the only issue). Never a batch-killing error.
    warnings: Tuple[str, ...] = ()


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
      ``confidence=high`` AND the category is in ``allowed_categories``
      (2026-08-10 review: severity is NOT an eligibility filter — real TPs
      are often minor; severity stays in the journal only).
    * ``REJECTED`` — deterministic false positive: never repaired.
    * ``TIER_B`` below the bar (low/medium confidence or category outside the
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
            if confidence == "high" and category in allowed_categories:
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

    Returns ``(results, errors, warnings)``:
    * ``errors`` — FATAL for the batch (debt, never a silent PASS): invalid
      JSON, unknown/duplicate/missing index, invalid decision, repair pid not
      matching the finding's pid, empty repaired_translation, truncated
      repair. ANY error fails the whole batch.
    * ``warnings`` — per-index NON-FATAL notices (no-op repairs converted to
      pass); the batch stays GOOD when they are the only issue.
    """
    errors: list = []
    warnings: list = []
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return (), (f"response is not valid JSON: {exc}",), ()
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
    ) -> None:
        self._repair_backend = repair_backend
        self._reaudit_backend = reaudit_backend or repair_backend
        self._config = config or SelectiveRepairConfig()

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
                continue
            for result in outcome.results:
                if result.decision == "repair":
                    committed[result.pid] = result.repaired_translation
                else:
                    # The model answers PASS by index without echoing the PID;
                    # resolve it from the batch finding (the index contract).
                    passed_pids.append(finding_by_index[result.index].pid)

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
        request = CompletionRequest(
            model_ref=model_ref,
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=cfg.max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=cfg.label,
            # NOTE: no request_options — the reasoning budget is a server arg.
        )
        try:
            response = self._repair_backend.complete(request)
        except Exception as exc:  # CompletionError and any transport-level failure
            LOG.error("repair batch transport failure (%s): %s", type(exc).__name__, exc)
            self._write_batch_artifacts(
                out_dir=out_dir, out_base=out_base, batch_index=batch_index,
                content=f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}\n",
                reasoning="",
            )
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
        for chunk_index, chunk_pairs in enumerate(chunks, start=1):
            chunk_pids = [p.pid for p in chunk_pairs]
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
            request = CompletionRequest(
                model_ref=model_ref,
                messages=(Message(role="user", content=prompt),),
                max_output_tokens=cfg.reaudit_max_tokens,
                temperature=0.0,
                response_schema=JSON_OBJECT_SCHEMA,
                label=cfg.reaudit_label,
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
                    # journal.
                    self._write_reaudit_artifacts(
                        out_dir=out_dir, out_base=out_base,
                        chunk_index=chunk_index,
                        content=f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}\n",
                        reasoning="",
                    )
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
                parsed = json.loads(content)
            except Exception as exc:
                return ReauditOutcome(
                    complete=False,
                    failed=True,
                    scope=scope_pids,
                    reason=f"re-audit chunk {chunk_index} response is not valid JSON: {exc}",
                )
            validation = validate_chunk_json(parsed, chunk_pids)
            if not validation.valid:
                errors.append(
                    f"chunk {chunk_index}: " + "; ".join(validation.errors)
                )
            all_issues.extend(validation.issues)
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
