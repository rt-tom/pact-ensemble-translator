"""Pact v4.1 repair package (B2).

``selective_repair`` — B2: selective repair (batch) + repair-as-verifier
over the generator (Gemma local / DeepSeek remote), with Tier A direct
repair, Tier B verify-before-repair, cap 100 per chapter (configurable),
microbatches of 3-4, and a single post-repair re-audit with bounded JSON
retry. Transport-neutral over
``CompletionBackend``; the lifecycle wrapper lives in
``pact_v4.runtime.model_lifecycle_adapters.LifecycleSelectiveRepairEvaluator``.
"""

from pact_v4.repair.selective_repair import (
    DEFAULT_REPAIR_CONTEXT_WINDOW,
    DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY,
    EligibleFinding,
    MICROBATCH_TARGET,
    MICROBATCH_TRIGGER,
    POLICY_LIMIT_TAG,
    REPAIR_FINDINGS_CAP,
    REPAIR_HARNESS_VERSION,
    REPAIR_PROMPT_VERSION,
    REPAIR_SCHEMA,
    ReauditOutcome,
    RepairBatchOutcome,
    RepairResult,
    SelectiveRepairConfig,
    SelectiveRepairEvaluator,
    SelectiveRepairOutcome,
    apply_findings_cap,
    make_microbatches,
    merge_candidates_by_pid,
    parse_repair_batch,
    plan_reaudit_scope,
    repair_model_ref,
    select_eligible,
)

__all__ = [
    "DEFAULT_REPAIR_CONTEXT_WINDOW",
    "DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY",
    "EligibleFinding",
    "MICROBATCH_TARGET",
    "MICROBATCH_TRIGGER",
    "POLICY_LIMIT_TAG",
    "REPAIR_FINDINGS_CAP",
    "REPAIR_HARNESS_VERSION",
    "REPAIR_PROMPT_VERSION",
    "REPAIR_SCHEMA",
    "ReauditOutcome",
    "RepairBatchOutcome",
    "RepairResult",
    "SelectiveRepairConfig",
    "SelectiveRepairEvaluator",
    "SelectiveRepairOutcome",
    "apply_findings_cap",
    "make_microbatches",
    "merge_candidates_by_pid",
    "parse_repair_batch",
    "plan_reaudit_scope",
    "repair_model_ref",
    "select_eligible",
]
