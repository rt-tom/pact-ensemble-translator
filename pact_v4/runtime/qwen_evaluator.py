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


# Qwen reviews a translation that is at most one chunk long. 4k tokens is
# well above the worst-case JSON response (a verdict with a short reason
# and a confidence label), and below any plausible chunk of 20 PIDs of
# English + Russian context.
DEFAULT_MAX_TOKENS = 4096


@dataclass(frozen=True)
class HttpQwenEvaluatorConfig:
    api: ApiClientConfig = field(default_factory=ApiClientConfig)
    max_tokens: int = DEFAULT_MAX_TOKENS
    label: str = "phase2c-qwen-fidelity"
    template: ReviewerPrompt = QWEN_FIDELITY_V1


_ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low"})


def _parse_qwen_verdict(raw: str) -> GateResult:
    """Parse the Qwen JSON verdict into a ``GateResult``; never crash."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return GateResult(
            gate="qwen_fidelity",
            passed=False,
            detail=f"qwen_fidelity: non-JSON response: {exc}; body={raw[:200]!r}",
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
        try:
            raw = self._api.complete(
                messages,
                max_tokens=self._max_tokens,
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
