"""Production-flavoured ``QwenEvaluator`` (Phase 2C Qwen fidelity gate).

The library's ``QwenEvaluator`` is a ``Protocol``: a callable that takes a
source ``PID -> EN text`` map and a translation ``PID -> RU text`` map and
returns a ``GateResult``. This module supplies one production-flavoured
implementation that:

* Renders the review request via the frozen
  ``pact_v4.runtime.prompts_runtime.QWEN_FIDELITY_V1`` template.
* Sends the request to the Qwen ``llama-server`` profile.
* Strictly parses the JSON reply; any non-JSON / wrong-shape response is
  surfaced as a failing ``GateResult`` whose ``detail`` records the raw
  reply for provenance. The cascade's contract is that a Qwen evaluator
  that cannot produce a verdict counts as a failed gate, not as an
  exception — the library's ``select_candidate`` already handles
  failures-by-gate.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from pact_v4.phase1.models import GateResult
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig, ApiClientError
from pact_v4.runtime.prompts_runtime import (
    QWEN_FIDELITY_V1,
    ReviewerPrompt,
    render_qwen_review_prompt,
)

LOG = logging.getLogger(__name__)


# Was 4096 ("well above the worst-case JSON response... below any
# plausible chunk of 20 PIDs"). That assumption was wrong: a live
# chapter_046 strict-driver trial (docs/plans/V4_STRICT_DRIVER_CHAPTER_TRIAL_TASK_RU.md,
# "Результат прогона") found Qwen's response truncated mid-JSON on a
# 32-PID chunk -- the raw body showed a clean pass
# (faithful/complete/no-errors/confidence=high) cut off mid-"reason"
# string -- and empty on a 44-PID chunk (budget likely exhausted inside
# the model's <think> block before any visible content). Both were
# scored as a failed gate indistinguishable from a real fidelity
# objection, inflating quarantine rates.
#
# 16384 is the *floor*, not a fixed value -- ChunkPlanner
# (pact_v4/phase1/chunker.py, DEFAULT_MAX_WORDS=640) bounds chunks by
# word count, not PID count, so a dialogue-heavy chunk (many short
# one-line PIDs) has no hard PID ceiling; chunk_plan.json from the
# chapter_046 trial already ranged 19-47 PIDs per chunk on ordinary
# prose. HttpQwenEvaluator.__call__ below scales max_tokens with
# len(translation) on top of this floor instead of trusting one static
# guess to cover an unbounded input shape.
DEFAULT_MAX_TOKENS = 16384

# Additional token budget per PID beyond the floor above, to keep pace
# with unusually large chunks instead of re-hitting the same truncation
# for the next chunk that happens to be bigger than whatever chunk
# calibrated the floor. Chosen so the observed 47-PID chunk
# (chapter_046, chunk0004) gets meaningfully more headroom than the
# floor alone: 47 * 128 = 6016, i.e. ~22k total for that chunk.
TOKENS_PER_PID = 128
# Upper bound so a pathological chunk doesn't request an output budget
# that leaves no room for the prompt within -c 32768 (the context size
# used by every profile in this codebase, see StrictBackendConfig /
# Measurement 2's SYCL profile). Not a proof the ceiling is always safe
# for arbitrarily large chunks -- if ChunkPlanner's word-based sizing
# ever produces a chunk whose prompt + this ceiling exceeds context,
# that is a chunk-sizing problem the gate-side token budget cannot fix,
# and should surface as a distinct failure mode rather than a silent
# truncation (see _parse_qwen_verdict's truncation detection).
MAX_TOKENS_CEILING = 24576


@dataclass(frozen=True)
class HttpQwenEvaluatorConfig:
    api: ApiClientConfig = field(default_factory=ApiClientConfig)
    max_tokens: int = DEFAULT_MAX_TOKENS
    label: str = "phase2c-qwen-fidelity"
    template: ReviewerPrompt = QWEN_FIDELITY_V1


_ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low"})


def _parse_qwen_verdict(raw: str) -> GateResult:
    """Parse the Qwen JSON verdict into a ``GateResult``; never crash.

    A JSON-parse failure is reported as a failed gate (never an
    exception), same as any other gate rejection -- but an empty body or
    a body that visibly stops mid-object/mid-string is very likely a
    ``max_tokens`` truncation (see PR discussion on ba396dd / the
    chapter_046 trial: a truncated response was observed mid-"reason"
    string, already showing a passing verdict before being cut off), not
    a genuine fidelity objection. That distinction is worth keeping
    visible in provenance/journal instead of collapsing into the same
    generic "non-JSON response" text, so a real future recurrence is
    diagnosable from the record alone, not only via a manual repro like
    ``v4_diag_qwen_truncation_repro.py``.
    """
    if not raw.strip():
        return GateResult(
            gate="qwen_fidelity",
            passed=False,
            detail="qwen_fidelity: empty response body (likely max_tokens exhausted "
            "before any visible content, e.g. inside a <think> block)",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        likely_truncated = (
            not raw.rstrip().endswith("}") or raw.count("{") > raw.count("}")
        )
        note = "likely truncated by max_tokens" if likely_truncated else "malformed JSON"
        return GateResult(
            gate="qwen_fidelity",
            passed=False,
            detail=f"qwen_fidelity: non-JSON response ({note}): {exc}; body={raw[:200]!r}",
        )
    if not isinstance(data, dict):
        return GateResult(
            gate="qwen_fidelity",
            passed=False,
            detail=f"qwen_fidelity: response is not a JSON object: {raw[:200]!r}",
        )

    confidence = str(data.get("confidence", "")).strip().casefold()
    if confidence not in _ALLOWED_CONFIDENCE:
        return GateResult(
            gate="qwen_fidelity",
            passed=False,
            detail=(
                f"qwen_fidelity: invalid confidence {confidence!r}; "
                f"body={raw[:200]!r}"
            ),
        )

    reason = str(data.get("reason", "")).strip() or "(no reason)"
    if "passed" in data:
        passed = bool(data["passed"])
    else:
        # The protocol says passed is "implied or explicit". When the
        # reviewer omits it, the implied verdict is "no introduced errors
        # AND faithful AND complete" — that is the only sensible default
        # that does not silently flip the cascade on a missing key.
        passed = (
            bool(data.get("faithful_to_source", False))
            and bool(data.get("completeness", False))
            and not bool(data.get("introduced_errors", True))
        )

    return GateResult(
        gate="qwen_fidelity",
        passed=passed,
        detail=(
            f"faithful={data.get('faithful_to_source')!r} "
            f"complete={data.get('completeness')!r} "
            f"errors={data.get('introduced_errors')!r} "
            f"confidence={confidence}; {reason}"
        ),
    )


class HttpQwenEvaluator:
    """Real ``QwenEvaluator`` backed by ``ApiClient``."""

    def __init__(
        self,
        api: Optional[ApiClient] = None,
        *,
        config: Optional[HttpQwenEvaluatorConfig] = None,
    ) -> None:
        if api is None and config is None:
            config = HttpQwenEvaluatorConfig()
        if api is None and config is not None:
            api = ApiClient(config.api, name=config.label)
        assert api is not None
        self._api = api
        self._config = config or HttpQwenEvaluatorConfig(api=api.config, label=api.name)
        self._max_tokens = int(self._config.max_tokens)

    @property
    def api(self) -> ApiClient:
        return self._api

    def __call__(
        self, source: Mapping[str, str], translation: Mapping[str, str]
    ) -> GateResult:
        prompt = render_qwen_review_prompt(
            source=dict(source),
            translation=dict(translation),
            template=self._config.template,
        )
        messages = [{"role": "user", "content": prompt}]
        # Floor (self._max_tokens, from config) + per-PID headroom, capped
        # at MAX_TOKENS_CEILING. See DEFAULT_MAX_TOKENS/TOKENS_PER_PID's
        # comments above for why a single static value doesn't fit every
        # chunk this evaluator gets called with.
        dynamic_max_tokens = min(
            MAX_TOKENS_CEILING, self._max_tokens + TOKENS_PER_PID * len(translation),
        )
        try:
            raw = self._api.complete(
                messages,
                max_tokens=dynamic_max_tokens,
                temperature=0.0,
                response_format_json=True,
                label="phase2c/qwen_fidelity",
            )
        except ApiClientError as exc:
            LOG.error("HttpQwenEvaluator: %s API failure: %s", self._api.name, exc)
            return GateResult(
                gate="qwen_fidelity",
                passed=False,
                detail=f"qwen_fidelity: API failure: {exc}",
            )
        return _parse_qwen_verdict(raw)
