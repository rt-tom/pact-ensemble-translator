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

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import (
    AuditPair,
    ChunkedAuditConfig,
    ChunkedAuditEvaluator,
    ChunkedAuditOutcome,
)
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
from pact_v4.runtime.json_resilience import JsonRetryPolicy
from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend
from pact_v4.runtime.model_caller import HttpModelCaller, HttpModelCallerConfig
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluator, HttpQwenEvaluatorConfig

GEMMA_MODEL_KEY = "gemma"
QWEN_MODEL_KEY = "qwen"


class LifecycleModelCaller:
    """``ModelCaller`` that ensures Gemma is resident before every call."""

    def __init__(self, router: ModelRouter, *, model_name: str,
                 config: Optional[HttpModelCallerConfig] = None,
                 json_retry_policy: Optional[JsonRetryPolicy] = None):
        self._router = router
        api_config = (config.api if config else ApiClientConfig()).__class__(
            chat_url=f"{router.base_url}/v1/chat/completions",
            model=model_name,
            timeout_seconds=(config.api.timeout_seconds if config else 1800.0),
            context_size=(config.api.context_size if config else 32768),
            temperature=(config.api.temperature if config else 0.2),
        )
        # A2 review fix (whole-chapter retry ownership): the generation-layer
        # policy (WholeChapterRetryPolicy) is the single retry owner in
        # whole-chapter mode, so the adapter-level JSON retry budget must be
        # disabled there (max_retries=0), exactly like the runtime-config
        # path does for build_role_adapters. When no policy is given the
        # historical default (JsonRetryPolicy(), max_retries=2) is kept, so
        # the chunked path keeps its current retry budget.
        retry = json_retry_policy if json_retry_policy is not None else JsonRetryPolicy()
        inner_config = HttpModelCallerConfig(
            api=api_config,
            max_tokens=(config.max_tokens if config else HttpModelCallerConfig().max_tokens),
            label=(config.label if config else HttpModelCallerConfig().label),
            retry=retry,
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
            bible_text=(config.bible_text if config else HttpQwenEvaluatorConfig().bible_text),
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

    B1 (v4.1 chunked audit): ``context_size`` was raised 32768 → 49152 to
    match the Qwen server's ``-c 49152`` (``runtime_local.example.yaml``,
    V4.1 §3.4) — the chunked audit's full input budget
    (fixed_prompt + narrator + entity + CONTEXT_ONLY + AUDIT_PAIRS) must fit
    the real server context. ``__call__`` is extended to accept the chunked
    inputs (``pairs`` + ``narrator_context``/``entity_context``) and returns
    a ``ChunkedAuditOutcome``; the legacy single-chunk call
    (``chunk_id``/``source``/``translation`` → raw text) is preserved for
    ``run_chapter_audit``'s per-chunk units.
    """

    def __init__(self, router: ModelRouter, *, model_name: str,
                 config: Optional[BackendQwenAuditEvaluatorConfig] = None):
        self._router = router
        cfg = config or BackendQwenAuditEvaluatorConfig()
        # temperature=0.0, not 0.2: the audit adapters always send
        # ``request.temperature == 0.0`` (same as the gate evaluations), so
        # keeping the ApiClientConfig in sync keeps the backend descriptor's
        # ``effective_options`` honest about what is actually sent.
        api_config = ApiClientConfig(
            chat_url=f"{router.base_url}/v1/chat/completions",
            model=model_name,
            timeout_seconds=1800.0,
            context_size=49152,
            temperature=0.0,
        )
        self._backend = LocalOpenAIBackend(api=ApiClient(api_config, name=cfg.label))
        self._evaluator = BackendQwenAuditEvaluator(
            self._backend,
            config=BackendQwenAuditEvaluatorConfig(
                max_tokens=cfg.max_tokens, template=cfg.template, label=cfg.label,
                bible_text=cfg.bible_text,
            ),
        )
        self._chunked = ChunkedAuditEvaluator(
            self._backend,
            config=ChunkedAuditConfig(label=cfg.label),
        )

    def __call__(
        self,
        *,
        chunk_id: str,
        source: Optional[Mapping[str, str]] = None,
        translation: Optional[Mapping[str, str]] = None,
        pairs: Optional[Sequence[AuditPair]] = None,
        narrator_context: str = "",
        entity_context: str = "",
        out_dir: Optional[Path] = None,
        out_base: str = "audit",
    ) -> Any:
        """Single-resident Qwen audit call.

        Legacy form (``source``+``translation``): returns the raw assistant
        text for one chunk (used by ``run_chapter_audit``).

        B1 chunked form (``pairs`` given): returns a ``ChunkedAuditOutcome``
        for the whole chapter — greedy chunking, CONTEXT_ONLY overlap,
        RetryShrink and fail-closed aggregation (``pact_v4.audit.
        chunked_audit``). ``out_dir``/``out_base`` persist per-chunk
        raw/reasoning artifacts (harness-compatible names).
        """
        self._router.ensure_resident(QWEN_MODEL_KEY)
        if pairs is not None:
            return self._chunked(
                chapter_id=chunk_id,
                pairs=pairs,
                narrator_context=narrator_context,
                entity_context=entity_context,
                out_dir=out_dir,
                out_base=out_base,
            )
        if source is None or translation is None:
            raise TypeError(
                "LifecycleQwenAuditEvaluator: either source+translation "
                "(legacy single chunk) or pairs (B1 chunked) must be given"
            )
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
            temperature=0.0,
        )
        backend = LocalOpenAIBackend(api=ApiClient(api_config, name=cfg.label))
        self._evaluator = BackendGemmaAuditEvaluator(
            backend,
            config=BackendGemmaAuditEvaluatorConfig(
                max_tokens=cfg.max_tokens, template=cfg.template, label=cfg.label,
                bible_text=cfg.bible_text,
            ),
        )

    def __call__(self, *, chunk_id: str, translation: Mapping[str, str]) -> str:
        self._router.ensure_resident(GEMMA_MODEL_KEY)
        return self._evaluator(chunk_id=chunk_id, translation=translation)
