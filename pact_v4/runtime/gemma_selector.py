"""Production-flavoured ``GemmaSelector`` (Phase 2C Gemma Russian preference).

The library's ``GemmaSelector`` is a ``Protocol``: a callable that takes a
sequence of ``(candidate_id, PID -> RU text)`` pairs (with NO English
source) and returns a ``GateResult`` whose ``detail`` carries the preferred
``candidate_id``. This module supplies one production-flavoured
implementation that:

* Renders the request via the frozen
  ``pact_v4.runtime.prompts_runtime.GEMMA_RUSSIAN_PREFERENCE_V1`` template.
* Sends the request to the Gemma ``llama-server`` profile.
* Strictly parses the JSON reply: an empty ``preferred_candidate_id``
  yields ``passed=False`` (the cascade's contract treats an undecided
  selector as a quarantine, never as a silent fallback).

The selector MUST be told which candidate_ids are valid — it cannot
fabricate a winner out of thin air. We validate ``preferred_candidate_id``
against the input set inside this class, exactly as
``pact_v4.phase2.cascade.select_candidate`` would do at the call site
(``_find_matching`` is an internal implementation detail there, so doing
the validation here too is defence in depth, not duplication).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import GateResult
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.prompts_runtime import (
    GEMMA_RUSSIAN_PREFERENCE_V1,
    ReviewerPrompt,
)


# Gemma sees N translations and emits a short JSON verdict. 1k tokens is
# well above the worst case (a preferred_candidate_id + a one-sentence
# reason).
DEFAULT_MAX_TOKENS = 1024


@dataclass(frozen=True)
class HttpGemmaSelectorConfig:
    api: ApiClientConfig = field(default_factory=ApiClientConfig)
    max_tokens: int = DEFAULT_MAX_TOKENS
    label: str = "phase2c-gemma-russian-preference"
    template: ReviewerPrompt = GEMMA_RUSSIAN_PREFERENCE_V1


def _parse_gemma_preference(
    raw: str, *, valid_candidate_ids: Sequence[str]
) -> GateResult:
    """Parse Gemma's JSON preference into a ``GateResult``.

    Validation rules (mirror the cascade's contract):

    * JSON parse error / non-object → ``passed=False``.
    * ``preferred_candidate_id`` empty or missing → ``passed=False``
      (cannot choose is not a silent fallback).
    * ``preferred_candidate_id`` not in ``valid_candidate_ids`` →
      ``passed=False`` (corrupted selector output).
    * Otherwise ``passed=True`` and ``detail`` carries the
      ``preferred_candidate_id``.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return GateResult(
            gate="gemma_russian_preference",
            passed=False,
            detail=(
                f"gemma_russian_preference: non-JSON response: {exc}; "
                f"body={raw[:200]!r}"
            ),
        )
    if not isinstance(data, dict):
        return GateResult(
            gate="gemma_russian_preference",
            passed=False,
            detail=(
                f"gemma_russian_preference: response is not a JSON object: "
                f"{raw[:200]!r}"
            ),
        )
    preferred = str(data.get("preferred_candidate_id", "")).strip()
    reason = str(data.get("reason", "")).strip() or "(no reason)"

    if not preferred:
        return GateResult(
            gate="gemma_russian_preference",
            passed=False,
            detail=f"gemma_russian_preference: no preference; {reason}",
        )
    if preferred not in valid_candidate_ids:
        return GateResult(
            gate="gemma_russian_preference",
            passed=False,
            detail=(
                f"gemma_russian_preference: preferred_candidate_id="
                f"{preferred!r} not in {list(valid_candidate_ids)}; {reason}"
            ),
        )
    return GateResult(
        gate="gemma_russian_preference",
        passed=True,
        detail=preferred,  # cascade reads this as the preferred id
    )


class HttpGemmaSelector:
    """Compatibility wrapper: real ``GemmaSelector`` backed by ``ApiClient``.

    Public constructor/property/behaviour are unchanged. Internally it
    delegates to the backend-neutral ``BackendGemmaSelector`` over a
    ``LocalOpenAIBackend`` (the V4 provider boundary). The import is lazy
    to avoid a module-level cycle (``backend_role_adapters`` imports the
    parsers from this module).
    """

    def __init__(
        self,
        api: Optional[ApiClient] = None,
        *,
        config: Optional[HttpGemmaSelectorConfig] = None,
    ) -> None:
        if api is None and config is None:
            config = HttpGemmaSelectorConfig()
        if api is None and config is not None:
            api = ApiClient(config.api, name=config.label)
        assert api is not None
        self._api = api
        self._config = config or HttpGemmaSelectorConfig(api=api.config, label=api.name)
        self._max_tokens = int(self._config.max_tokens)
        from pact_v4.runtime.backend_role_adapters import (
            BackendGemmaSelector,
            BackendGemmaSelectorConfig,
        )
        from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend

        self._impl = BackendGemmaSelector(
            LocalOpenAIBackend(api=api),
            config=BackendGemmaSelectorConfig(
                max_tokens=self._max_tokens,
                template=self._config.template,
            ),
        )

    @property
    def api(self) -> ApiClient:
        return self._api

    @property
    def impl(self) -> "BackendGemmaSelector":
        return self._impl

    def __call__(
        self, candidates: Sequence[Tuple[str, Mapping[str, str]]]
    ) -> GateResult:
        return self._impl(candidates)
