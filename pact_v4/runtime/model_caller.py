"""Production-flavoured ``ModelCaller`` (Phase 2B A/B generation).

Wires the library's abstract ``ModelCaller`` protocol to a real
``llama-server`` chat-completions endpoint. The default model is Gemma 4
26B (same as the v3 production translator) but the constructor accepts any
``ApiClientConfig`` so a Qwen-as-generator experiment is one config change
away.

The role this class plays is the *opposite* of what Phase 2B production
caching does: it has no opinion about caching — the library owns
``GenerationCache`` and this adapter is consulted only on a miss. We do
not duplicate the library's contract here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pact_v4.phase2.generation import PromptBundle
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig, ApiClientError
from pact_v4.runtime.backend_protocol import CompletionError
from pact_v4.runtime.backend_role_adapters import BackendModelCaller, BackendModelCallerConfig
from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend


# Phase 2B calls are JSON-object output with chunk-sized max_tokens. The
# upper bound is generous (8k is well above what a single 20-PID chunk
# needs at the provisional temperatures) but leaves headroom for any
# future A/B template that may need to emit more verbose JSON.
DEFAULT_MAX_TOKENS = 8192


@dataclass(frozen=True)
class HttpModelCallerConfig:
    api: ApiClientConfig = field(default_factory=ApiClientConfig)
    max_tokens: int = DEFAULT_MAX_TOKENS
    label: str = "phase2b-generation"


class HttpModelCaller:
    """Compatibility wrapper: real ``ModelCaller`` backed by ``ApiClient``.

    Public constructor/property/behaviour are unchanged. Internally it now
    delegates to the backend-neutral ``BackendModelCaller`` over a
    ``LocalOpenAIBackend`` (the V4 provider boundary), so the role no
    longer depends on the HTTP protocol directly.

    Implements the ``ModelCaller`` protocol by rendering the bundle into a
    single user message, sending it to the backend, and returning the raw
    assistant text. JSON validation, PID-set enforcement, and cache
    identity all live in ``pact_v4.phase2.generation`` — this class does
    not duplicate any of that.
    """

    def __init__(
        self,
        api: Optional[ApiClient] = None,
        *,
        config: Optional[HttpModelCallerConfig] = None,
    ) -> None:
        if api is None and config is None:
            config = HttpModelCallerConfig()
        if api is None and config is not None:
            api = ApiClient(config.api, name=config.label)
        assert api is not None  # narrowed by the two branches above
        self._api = api
        self._config = config or HttpModelCallerConfig(api=api.config, label=api.name)
        self._max_tokens = int(self._config.max_tokens)
        self._backend = LocalOpenAIBackend(api=api)
        self._impl = BackendModelCaller(
            self._backend,
            config=BackendModelCallerConfig(max_tokens=self._max_tokens),
        )

    @property
    def api(self) -> ApiClient:
        return self._api

    @property
    def backend(self) -> LocalOpenAIBackend:
        return self._backend

    def __call__(self, bundle: PromptBundle) -> str:
        try:
            return self._impl(bundle)
        except CompletionError as exc:
            # Compatibility contract: the public wrapper keeps raising
            # ApiClientError (its pre-boundary behaviour). The backend
            # boundary raises CompletionError; restore the legacy type.
            cause = exc.__cause__
            if isinstance(cause, ApiClientError):
                raise cause
            raise ApiClientError(str(exc)) from exc
