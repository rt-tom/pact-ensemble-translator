"""Lifecycle-aware wrappers around the production ``Http*`` adapters.

``pact_v4.runtime.model_caller.HttpModelCaller``,
``pact_v4.runtime.qwen_evaluator.HttpQwenEvaluator``, and
``pact_v4.runtime.gemma_selector.HttpGemmaSelector`` all assume the right
model is already resident behind their fixed ``chat_url``. On single-GPU
hardware that is only true if something swaps the model before the call.

These three wrappers add exactly that: each is a drop-in
``ModelCaller``/``QwenEvaluator``/``GemmaSelector`` (same call signature,
so ``pact_v4.phase2.cascade.select_candidate`` and
``pact_v4.pipeline.v4_phase12_draft_runner.run_chapter`` need no changes)
that calls ``ModelRouter.ensure_resident(...)`` before delegating to the
real ``Http*`` instance. ``ensure_resident`` is a no-op when the requested
model is already loaded, so two calls in a row that both need Gemma
(``Gpref(N)`` then ``Ggen(N+1)``) never trigger a second restart -- this
is what makes the strict driver's restart accounting match the
architecture doc's "Подсчёт перезапусков" table instead of the bench
script's always-restart segment plan.

None of the gate/decision logic in ``pact_v4.phase2.cascade`` or the
prompt-rendering in ``pact_v4.runtime.prompts_runtime`` is touched here --
this module only decides *when to swap*, never *what the model is asked*.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import GateResult
from pact_v4.phase2.generation import PromptBundle
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaAuditEvaluator,
    BackendGemmaAuditEvaluatorConfig,
    BackendQwenAuditEvaluator,
    BackendQwenAuditEvaluatorConfig,
)
from pact_v4.runtime.gemma_selector import HttpGemmaSelector, HttpGemmaSelectorConfig
from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend
from pact_v4.runtime.model_caller import HttpModelCaller, HttpModelCallerConfig
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluator, HttpQwenEvaluatorConfig

GEMMA_MODEL_KEY = "gemma"
QWEN_MODEL_KEY = "qwen"


class LifecycleModelCaller:
    """``ModelCaller`` that ensures Gemma is resident before every call."""

    def __init__(self, router: ModelRouter, *, model_name: str,
                 config: Optional[HttpModelCallerConfig] = None):
        self._router = router
        api_config = (config.api if config else ApiClientConfig()).__class__(
            chat_url=f"{router.base_url}/v1/chat/completions",
            model=model_name,
            timeout_seconds=(config.api.timeout_seconds if config else 1800.0),
            context_size=(config.api.context_size if config else 32768),
            temperature=(config.api.temperature if config else 0.2),
        )
        inner_config = HttpModelCallerConfig(
            api=api_config,
            max_tokens=(config.max_tokens if config else HttpModelCallerConfig().max_tokens),
            label=(config.label if config else HttpModelCallerConfig().label),
        )
        self._caller = HttpModelCaller(config=inner_config)

    def __call__(self, bundle: PromptBundle) -> str:
        self._router.ensure_resident(GEMMA_MODEL_KEY)
        return self._caller(bundle)


class LifecycleQwenEvaluator:
    """``QwenEvaluator`` that ensures Qwen is resident before every call."""

    def __init__(self, router: ModelRouter, *, model_name: str,
                 config: Optional[HttpQwenEvaluatorConfig] = None):
        self._router = router
        api_config = ApiClientConfig(
            chat_url=f"{router.base_url}/v1/chat/completions",
            model=model_name,
            timeout_seconds=(config.api.timeout_seconds if config else 1800.0),
            context_size=(config.api.context_size if config else 32768),
            temperature=(config.api.temperature if config else 0.2),
        )
        inner_config = HttpQwenEvaluatorConfig(
            api=api_config,
            max_tokens=(config.max_tokens if config else HttpQwenEvaluatorConfig().max_tokens),
            label=(config.label if config else HttpQwenEvaluatorConfig().label),
            template=(config.template if config else HttpQwenEvaluatorConfig().template),
        )
        self._evaluator = HttpQwenEvaluator(config=inner_config)

    def __call__(self, source: Mapping[str, str], translation: Mapping[str, str]) -> GateResult:
        self._router.ensure_resident(QWEN_MODEL_KEY)
        return self._evaluator(source, translation)


class LifecycleGemmaSelector:
    """``GemmaSelector`` that ensures Gemma is resident before every call."""

    def __init__(self, router: ModelRouter, *, model_name: str,
                 config: Optional[HttpGemmaSelectorConfig] = None):
        self._router = router
        api_config = ApiClientConfig(
            chat_url=f"{router.base_url}/v1/chat/completions",
            model=model_name,
            timeout_seconds=(config.api.timeout_seconds if config else 1800.0),
            context_size=(config.api.context_size if config else 32768),
            temperature=(config.api.temperature if config else 0.2),
        )
        inner_config = HttpGemmaSelectorConfig(
            api=api_config,
            max_tokens=(config.max_tokens if config else HttpGemmaSelectorConfig().max_tokens),
            label=(config.label if config else HttpGemmaSelectorConfig().label),
            template=(config.template if config else HttpGemmaSelectorConfig().template),
        )
        self._selector = HttpGemmaSelector(config=inner_config)

    def __call__(
        self, candidates: Sequence[Tuple[str, Mapping[str, str]]],
    ) -> GateResult:
        self._router.ensure_resident(GEMMA_MODEL_KEY)
        return self._selector(candidates)


class LifecycleQwenAuditEvaluator:
    """``QwenAuditEvaluator`` (Phase 3B Step 6) over the router's Qwen.

    Same single-resident contract as the other ``Lifecycle*`` wrappers: the
    audit adapters from ``backend_role_adapters`` are transport-neutral, so
    this wrapper supplies the local ``llama-server`` transport
    (``LocalOpenAIBackend`` over an ``ApiClient`` pointed at the router's
    base URL) and ensures Qwen is resident before every call. The strict
    driver's audit phase pays the batching benefit of
    ``run_chapter_audit``'s detector-outer loop: one acquire for all Qwen
    units, then one switch to Gemma for all Gemma units.
    """

    def __init__(self, router: ModelRouter, *, model_name: str,
                 config: Optional[BackendQwenAuditEvaluatorConfig] = None):
        self._router = router
        cfg = config or BackendQwenAuditEvaluatorConfig()
        api_config = ApiClientConfig(
            chat_url=f"{router.base_url}/v1/chat/completions",
            model=model_name,
            timeout_seconds=1800.0,
            context_size=32768,
            temperature=0.2,
        )
        backend = LocalOpenAIBackend(api=ApiClient(api_config, name=cfg.label))
        self._evaluator = BackendQwenAuditEvaluator(
            backend,
            config=BackendQwenAuditEvaluatorConfig(
                max_tokens=cfg.max_tokens, template=cfg.template, label=cfg.label,
            ),
        )

    def __call__(
        self, *, chunk_id: str, source: Mapping[str, str], translation: Mapping[str, str]
    ) -> str:
        self._router.ensure_resident(QWEN_MODEL_KEY)
        return self._evaluator(chunk_id=chunk_id, source=source, translation=translation)


class LifecycleGemmaAuditEvaluator:
    """``GemmaAuditEvaluator`` (Phase 3B Step 6) over the router's Gemma.

    Russian-only review: the wrapped ``BackendGemmaAuditEvaluator`` is never
    given the English source.
    """

    def __init__(self, router: ModelRouter, *, model_name: str,
                 config: Optional[BackendGemmaAuditEvaluatorConfig] = None):
        self._router = router
        cfg = config or BackendGemmaAuditEvaluatorConfig()
        api_config = ApiClientConfig(
            chat_url=f"{router.base_url}/v1/chat/completions",
            model=model_name,
            timeout_seconds=1800.0,
            context_size=32768,
            temperature=0.2,
        )
        backend = LocalOpenAIBackend(api=ApiClient(api_config, name=cfg.label))
        self._evaluator = BackendGemmaAuditEvaluator(
            backend,
            config=BackendGemmaAuditEvaluatorConfig(
                max_tokens=cfg.max_tokens, template=cfg.template, label=cfg.label,
            ),
        )

    def __call__(self, *, chunk_id: str, translation: Mapping[str, str]) -> str:
        self._router.ensure_resident(GEMMA_MODEL_KEY)
        return self._evaluator(chunk_id=chunk_id, translation=translation)
