"""Backend-neutral role adapters over ``CompletionBackend``.

These are the Phase 2 model-role adapters (generator, Qwen fidelity
reviewer, Gemma Russian selector) re-wired from ``ApiClient.complete(...)``
to ``CompletionBackend.complete(request)`` — the mechanical replacement
described in the V4 integration plan (§6). They reuse the exact same
prompt rendering, parsers and validation as the previous ``Http*``
classes:

* ``render_prompt`` for generation (Phase 2B);
* ``render_qwen_review_prompt`` / ``_parse_qwen_verdict`` for the Qwen
  fidelity gate;
* ``render_gemma_preference_prompt`` / ``_parse_gemma_preference`` for the
  Gemma Russian-preference gate;
* existing validation lives in ``pact_v4.phase2.generation`` (unchanged).

A model role no longer depends on a concrete HTTP protocol: any
``CompletionBackend`` (local, remote, composite) can serve these roles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import GateResult
from pact_v4.phase2.generation import ModelCaller, PromptBundle
from pact_v4.phase2.prompts import render_prompt
from pact_v4.runtime.backend_protocol import (
    JSON_OBJECT_SCHEMA,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    Message,
)
from pact_v4.runtime.gemma_selector import _parse_gemma_preference
from pact_v4.runtime.prompts_runtime import (
    GEMMA_AUDIT_V1,
    GEMMA_RUSSIAN_PREFERENCE_V1,
    QWEN_AUDIT_V1,
    QWEN_FIDELITY_V1,
    ReviewerPrompt,
    render_gemma_audit_prompt,
    render_gemma_preference_prompt,
    render_qwen_audit_prompt,
    render_qwen_review_prompt,
)
from pact_v4.runtime.qwen_evaluator import (
    MAX_TOKENS_CEILING,
    TOKENS_PER_PID,
    _parse_qwen_verdict,
)

LOG = logging.getLogger(__name__)


# Phase 2B calls are JSON-object output with chunk-sized max_tokens. The
# upper bound is generous (8k is well above what a single 20-PID chunk
# needs at the provisional temperatures) but leaves headroom for any
# future A/B template that may need to emit more verbose JSON.
DEFAULT_MAX_TOKENS = 8192


def _model_ref_for(backend: CompletionBackend, roles: Sequence[str]) -> str:
    """Resolve the role → model binding from the backend descriptor.

    Falls back to a ``default`` binding. Raises a role-aware ``ValueError``
    when no binding exists so a role without an assigned model fails loudly
    instead of silently using whatever model the transport is serving.
    """
    bindings = backend.descriptor.model_bindings
    for role in roles:
        ref = bindings.get(role)
        if ref:
            return ref
    ref = bindings.get("default")
    if ref:
        return ref
    raise ValueError(
        f"no model binding for role(s) {list(roles)!r}; "
        f"backend model_bindings={dict(bindings)!r}"
    )


@dataclass(frozen=True)
class BackendModelCallerConfig:
    max_tokens: int = DEFAULT_MAX_TOKENS
    label: str = "phase2b-generation"


class BackendModelCaller:
    """``ModelCaller`` protocol implementation over a ``CompletionBackend``.

    Renders the bundle into a single user message, sends it through the
    backend, and returns the raw assistant text. JSON validation, PID-set
    enforcement, and cache identity all live in ``pact_v4.phase2.generation``
    — this class does not duplicate any of that.
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendModelCallerConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendModelCallerConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(self, bundle: PromptBundle) -> str:
        user_text = render_prompt(bundle)
        # Generation bundle roles are the template roles ("fidelity_first" /
        # "balanced_literary"), while tagged configs bind the model under the
        # plan §8 alias "generator". Resolve either, then fall back to a
        # "default" binding (PR 4 / C3: makes the plan's config shape work
        # without forcing every config to duplicate the bundle role names).
        # The alias is generation-only by design: the other four role
        # adapters resolve via the v4 gate names ("fidelity_reviewer",
        # "russian_selector", "qwen_audit", "gemma_audit") which configs bind
        # verbatim, so they need no alias namespace. If a config ever binds
        # "fidelity_first"/"balanced_literary" to a different model than
        # "generator", this lookup honours the bundle role first and the
        # alias only as a fallback.
        request = CompletionRequest(
            model_ref=_model_ref_for(self._backend, (bundle.role, "generator")),
            messages=(Message(role="user", content=user_text),),
            max_output_tokens=self._max_tokens,
            temperature=bundle.params.temperature,
            response_schema=JSON_OBJECT_SCHEMA,
            label=f"phase2b/{bundle.role}/{bundle.chunk_id}",
        )
        try:
            response = self._backend.complete(request)
        except CompletionError as exc:
            LOG.error("BackendModelCaller: backend failure: %s", exc)
            raise
        return response.text


@dataclass(frozen=True)
class BackendQwenEvaluatorConfig:
    max_tokens: int = 16384
    template: ReviewerPrompt = QWEN_FIDELITY_V1


class BackendQwenEvaluator:
    """``QwenEvaluator`` protocol implementation over a ``CompletionBackend``."""

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendQwenEvaluatorConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendQwenEvaluatorConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(
        self, source: Mapping[str, str], translation: Mapping[str, str]
    ) -> GateResult:
        prompt = render_qwen_review_prompt(
            source=dict(source),
            translation=dict(translation),
            template=self._config.template,
        )
        # Floor (config.max_tokens) + per-PID headroom, capped at
        # MAX_TOKENS_CEILING — see qwen_evaluator.py for the rationale.
        dynamic_max_tokens = min(
            MAX_TOKENS_CEILING, self._max_tokens + TOKENS_PER_PID * len(translation),
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(
                self._backend, ("fidelity_reviewer", "qwen_fidelity")
            ),
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=dynamic_max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label="phase2c/qwen_fidelity",
        )
        try:
            response = self._backend.complete(request)
        except CompletionError as exc:
            LOG.error("BackendQwenEvaluator: backend failure: %s", exc)
            return GateResult(
                gate="qwen_fidelity",
                passed=False,
                detail=f"qwen_fidelity: API failure: {exc}",
            )
        return _parse_qwen_verdict(response.text)


@dataclass(frozen=True)
class BackendGemmaSelectorConfig:
    max_tokens: int = 1024
    template: ReviewerPrompt = GEMMA_RUSSIAN_PREFERENCE_V1


class BackendGemmaSelector:
    """``GemmaSelector`` protocol implementation over a ``CompletionBackend``."""

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendGemmaSelectorConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendGemmaSelectorConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(
        self, candidates: Sequence[Tuple[str, Mapping[str, str]]]
    ) -> GateResult:
        valid_ids = [cid for cid, _ in candidates]
        if not valid_ids:
            return GateResult(
                gate="gemma_russian_preference",
                passed=False,
                detail="gemma_russian_preference: empty candidate set",
            )
        prompt = render_gemma_preference_prompt(
            candidates=[(cid, dict(mapping)) for cid, mapping in candidates],
            template=self._config.template,
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(
                self._backend, ("russian_selector", "gemma_russian_preference")
            ),
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=self._max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label="phase2c/gemma_russian_preference",
        )
        try:
            response = self._backend.complete(request)
        except CompletionError as exc:
            LOG.error("BackendGemmaSelector: backend failure: %s", exc)
            return GateResult(
                gate="gemma_russian_preference",
                passed=False,
                detail=f"gemma_russian_preference: API failure: {exc}",
            )
        return _parse_gemma_preference(response.text, valid_candidate_ids=valid_ids)


@dataclass(frozen=True)
class BackendQwenAuditEvaluatorConfig:
    """Step 6 Qwen audit call settings.

    ``max_tokens`` is the *floor* (the Qwen ``max_tokens`` fix, PR #96):
    the audit output for a large chunk can be long, and truncation mid-JSON
    would otherwise surface as a spurious fidelity objection. Per-PID
    headroom is added on top exactly like the fidelity gate
    (``TOKENS_PER_PID``), capped at ``MAX_TOKENS_CEILING``.
    """

    max_tokens: int = 16384
    template: ReviewerPrompt = QWEN_AUDIT_V1
    label: str = "phase3/qwen_chapter_audit"


class BackendQwenAuditEvaluator:
    """``pact_v4.phase3.audit.QwenAuditEvaluator`` over a ``CompletionBackend``.

    Transport-only role adapter (V4 A1 pattern): renders the Qwen audit
    prompt, sends it through ``backend.complete(request)``, and returns the
    raw assistant text. Issue parsing/validation and per-unit resumability
    live in ``pact_v4.phase3.audit``, unchanged. A transport failure raises
    ``CompletionError`` — ``run_chapter_audit`` converts any exception into a
    failed (resumable) unit, so the audit can never claim ``complete`` on a
    model failure.
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendQwenAuditEvaluatorConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendQwenAuditEvaluatorConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(
        self, *, chunk_id: str, source: Mapping[str, str], translation: Mapping[str, str]
    ) -> str:
        prompt = render_qwen_audit_prompt(
            chunk_id=chunk_id,
            source=dict(source),
            translation=dict(translation),
            template=self._config.template,
        )
        # Floor (config.max_tokens) + per-PID headroom, capped at
        # MAX_TOKENS_CEILING — same Qwen max_tokens fix as the fidelity
        # gate (see qwen_evaluator.py for the rationale).
        dynamic_max_tokens = min(
            MAX_TOKENS_CEILING, self._max_tokens + TOKENS_PER_PID * len(translation),
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(
                self._backend, ("qwen_audit", "fidelity_reviewer", "qwen_fidelity")
            ),
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=dynamic_max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=self._config.label,
        )
        try:
            response = self._backend.complete(request)
        except CompletionError as exc:
            LOG.error("BackendQwenAuditEvaluator: backend failure: %s", exc)
            raise
        return response.text


@dataclass(frozen=True)
class BackendGemmaAuditEvaluatorConfig:
    max_tokens: int = 4096
    template: ReviewerPrompt = GEMMA_AUDIT_V1
    label: str = "phase3/gemma_russian_review"


class BackendGemmaAuditEvaluator:
    """``pact_v4.phase3.audit.GemmaAuditEvaluator`` over a ``CompletionBackend``.

    Russian-only review: never given the English source (spec
    "Russian-only review без оригинала"), matching the protocol signature
    exactly. Transport failures raise ``CompletionError`` for
    ``run_chapter_audit`` to record as a failed unit.
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendGemmaAuditEvaluatorConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendGemmaAuditEvaluatorConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(self, *, chunk_id: str, translation: Mapping[str, str]) -> str:
        prompt = render_gemma_audit_prompt(
            chunk_id=chunk_id,
            translation=dict(translation),
            template=self._config.template,
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(
                self._backend, ("gemma_audit", "russian_selector", "gemma_russian_preference")
            ),
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=self._max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=self._config.label,
        )
        try:
            response = self._backend.complete(request)
        except CompletionError as exc:
            LOG.error("BackendGemmaAuditEvaluator: backend failure: %s", exc)
            raise
        return response.text


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "BackendModelCallerConfig",
    "BackendModelCaller",
    "BackendQwenEvaluatorConfig",
    "BackendQwenEvaluator",
    "BackendGemmaSelectorConfig",
    "BackendGemmaSelector",
    "BackendQwenAuditEvaluatorConfig",
    "BackendQwenAuditEvaluator",
    "BackendGemmaAuditEvaluatorConfig",
    "BackendGemmaAuditEvaluator",
]
