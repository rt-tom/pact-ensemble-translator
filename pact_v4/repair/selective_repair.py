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
  unknown/duplicate/missing index, invalid decision, no-op repair) is debt,
  NEVER a silent PASS. A failed re-audit is debt, never ``0 findings``.
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
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import (
    AuditPair,
    audit_model_ref,
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
REPAIR_PROMPT_VERSION = "pact-v4-repair-as-verifier/v1"

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
# changed PID, in source order) and the full-re-audit threshold (changed PID
# count above which the re-audit covers the whole chapter).
DEFAULT_REAUDIT_NEIGHBOUR_WINDOW = 2
DEFAULT_REAUDIT_FULL_THRESHOLD = 8

# Repair output budget (per batch call) and re-audit output budget. The
# re-audit input profile is the SAME as the extractor (full source + full
# current translation), so it shares the extractor's 20000-token budget
# (8192 reasoning + content headroom; 12000 was exhausted by reasoning on
# the full input in run_010-style chapters — owner decision 2026-08-11).
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
    reaudit_enabled: bool = True
    reaudit_neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW
    reaudit_full_threshold: int = DEFAULT_REAUDIT_FULL_THRESHOLD
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


@dataclass(frozen=True)
class ReauditOutcome:
    """Outcome of the single post-repair re-audit call.

    ``complete`` is True when the call, JSON parse and scope validation all
    succeeded (``issues`` then carries what the re-audit found in scope);
    ``failed`` is True when any of those failed — the caller must treat that
    as debt and NEVER claim ``0 findings`` (fail-closed, Phase 0/A1c fix).
    """

    complete: bool
    failed: bool
    issues: Tuple[Mapping[str, Any], ...] = ()
    scope: Tuple[str, ...] = ()
    full: bool = False
    reason: str = ""


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
                }
                for b in self.batches
            ],
            "committed": [list(pair) for pair in self.committed],
            "passed_pids": list(self.passed_pids),
            "debt_trace": list(self.debt_trace),
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
) -> Tuple[Tuple[RepairResult, ...], Tuple[str, ...]]:
    """Parse and strictly validate one repair batch response.

    Fail-closed contract: the response must be a JSON object with a
    ``results`` array; every finding ``[index]`` must be answered exactly
    once; each result must carry ``decision`` ``pass``|``repair``; a repair
    must name the EXACT PID of the finding that ``index`` refers to (the
    index/PID contract — a repair naming any other batch target would commit
    the fix to the wrong paragraph) and a NON-EMPTY ``repaired_translation``
    that actually differs from the current text (a no-op "repair" is a
    contract violation -> error). When several findings share one PID each
    index is still validated against its own finding's PID, so a shared-PID
    group is answered per index exactly like a distinct-PID one. ANY error
    -> the whole batch is failed (debt), never a silent PASS.
    """
    errors: list = []
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return (), (f"response is not valid JSON: {exc}",)
    if not isinstance(parsed, dict) or "results" not in parsed:
        return (), ("root object has no 'results' array",)
    results = parsed.get("results")
    if not isinstance(results, list):
        return (), ("'results' is not an array",)
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
            errors.append(
                f"index {index}: repaired_translation equals the current text "
                f"(no-op repair is a contract violation — use decision 'pass')"
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
    return tuple(out), tuple(errors)


# ---------------------------------------------------------------------------
# Re-audit scope planning (pure)
# ---------------------------------------------------------------------------


def plan_reaudit_scope(
    changed_pids: Sequence[str],
    all_pids: Sequence[str],
    *,
    neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW,
    full_threshold: int = DEFAULT_REAUDIT_FULL_THRESHOLD,
) -> Tuple[Tuple[str, ...], bool]:
    """Scope of the single post-repair re-audit: ``(pids, full)``.

    Default: the changed PIDs plus their neighbour window (``window`` PIDs
    before/after each in source order), in source order — a single Qwen call.
    When the number of changed PIDs exceeds ``full_threshold`` the scope
    becomes the whole chapter (``full=True``), per the architecture plan
    §10 B2.4 ("при превышении порога изменённых регионов — один full
    re-audit").
    """
    if len(changed_pids) > full_threshold:
        return tuple(all_pids), True
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
    return tuple(pid for pid in all_pids if pid in scope), False


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
        on_phase: Optional[Callable[[str], None]] = None,
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

        reaudit: Optional[ReauditOutcome] = None
        repair_complete = all(b.status == "GOOD" for b in batch_outcomes)
        if committed and cfg.reaudit_enabled:
            phase("reaudit")
            reaudit = self._run_reaudit(
                chapter_id=chapter_id,
                source=source,
                translation=dict(translation, **committed),
                changed_pids=tuple(committed),
                entity_context=entity_context,
                narrator_context=narrator_context,
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
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _run_batch(
        self,
        *,
        chapter_id: str,
        source: Mapping[str, str],
        translation: Mapping[str, str],
        findings: Sequence[EligibleFinding],
        batch_index: int,
    ) -> RepairBatchOutcome:
        cfg = self._config
        prompt = render_selective_repair_prompt(
            chapter_id=chapter_id,
            source=dict(source),
            translation=dict(translation),
            findings=findings,
            template=cfg.template,
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
            return RepairBatchOutcome(
                batch_index=batch_index,
                status="FAILED",
                findings=tuple(findings),
                error=f"{type(exc).__name__}: {exc}",
            )
        results, errors = parse_repair_batch(
            response.text or "", findings, current_by_pid=dict(translation)
        )
        if errors:
            return RepairBatchOutcome(
                batch_index=batch_index,
                status="FAILED",
                findings=tuple(findings),
                results=results,
                error="; ".join(errors),
            )
        return RepairBatchOutcome(
            batch_index=batch_index,
            status="GOOD",
            findings=tuple(findings),
            results=results,
        )

    def _run_reaudit(
        self,
        *,
        chapter_id: str,
        source: Mapping[str, str],
        translation: Mapping[str, str],
        changed_pids: Sequence[str],
        entity_context: str,
        narrator_context: str = "",
    ) -> ReauditOutcome:
        cfg = self._config
        try:
            pairs = pairs_from_maps(source, translation)
        except Exception as exc:
            return ReauditOutcome(
                complete=False, failed=True, reason=f"pair construction failed: {exc}"
            )
        scope_pids, full = plan_reaudit_scope(
            changed_pids,
            [p.pid for p in pairs],
            neighbour_window=cfg.reaudit_neighbour_window,
            full_threshold=cfg.reaudit_full_threshold,
        )
        by_pid = {p.pid: p for p in pairs}
        if full:
            audit_pairs = pairs
            context_pairs: Tuple[AuditPair, ...] = ()
        else:
            audit_pairs = tuple(by_pid[pid] for pid in scope_pids if pid in by_pid)
            # Full-chapter input (architecture plan §10 B2.4: "вход = полный
            # source + полная translation"): every pair OUTSIDE the reportable
            # scope is supplied as CONTEXT_ONLY so the model can resolve
            # distant speakers/referents/continuity; validation still rejects
            # any issue reported outside ``scope_pids`` (fail-closed scope).
            context_pairs = tuple(p for p in pairs if p.pid not in scope_pids)
        prompt = render_reaudit_prompt(
            chapter_id=chapter_id,
            audit_pairs=audit_pairs,
            context_pairs=context_pairs,
            narrator_context=narrator_context,
            entity_context=entity_context,
        )
        model_ref = audit_model_ref(self._reaudit_backend)
        request = CompletionRequest(
            model_ref=model_ref,
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=cfg.reaudit_max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=cfg.reaudit_label,
        )

        def _complete() -> str:
            # Re-issues the IDENTICAL request on a retry (same prompt, same
            # max_output_tokens, same model_ref, same backend — B4 §4), so a
            # retry never changes cache/resume identity. A transport failure
            # (CompletionError or any subclass) propagates immediately and is
            # never retried as a JSON problem (B4 §1/§3).
            try:
                return self._reaudit_backend.complete(request).text or ""
            except CompletionError as exc:
                LOG.error("re-audit transport failure (%s): %s", type(exc).__name__, exc)
                raise

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
                full=full,
                reason=f"re-audit response invalid after "
                       f"{cfg.reaudit_retry.max_retries + 1} attempt(s): {exc}",
            )
        except CompletionError as exc:
            return ReauditOutcome(
                complete=False,
                failed=True,
                scope=scope_pids,
                full=full,
                reason=f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — any transport-level failure is debt, never a crash
            LOG.error("re-audit unexpected failure (%s): %s", type(exc).__name__, exc)
            return ReauditOutcome(
                complete=False,
                failed=True,
                scope=scope_pids,
                full=full,
                reason=f"{type(exc).__name__}: {exc}",
            )
        try:
            parsed = json.loads(content)
        except Exception as exc:
            return ReauditOutcome(
                complete=False,
                failed=True,
                scope=scope_pids,
                full=full,
                reason=f"re-audit response is not valid JSON: {exc}",
            )
        validation = validate_chunk_json(parsed, scope_pids)
        if not validation.valid:
            return ReauditOutcome(
                complete=False,
                failed=True,
                scope=scope_pids,
                full=full,
                issues=validation.issues,
                reason="; ".join(validation.errors),
            )
        return ReauditOutcome(
            complete=True,
            failed=False,
            issues=validation.issues,
            scope=scope_pids,
            full=full,
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
    "MICROBATCH_TRIGGER",
    "MICROBATCH_TARGET",
    "SelectiveRepairConfig",
    "EligibleFinding",
    "RepairResult",
    "RepairBatchOutcome",
    "ReauditOutcome",
    "SelectiveRepairOutcome",
    "repair_model_ref",
    "select_eligible",
    "apply_findings_cap",
    "make_microbatches",
    "parse_repair_batch",
    "plan_reaudit_scope",
    "SelectiveRepairEvaluator",
]
