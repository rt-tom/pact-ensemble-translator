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
from pact_v4.runtime.json_resilience import (
    JsonRetryPolicy,
    retry_json_call,
)
from pact_v4.runtime.prompts_runtime import (
    FORMAT_SPANS_V1,
    GEMMA_AUDIT_V1,
    GEMMA_RUSSIAN_PREFERENCE_V1,
    QWEN_AUDIT_V1,
    QWEN_FIDELITY_V1,
    REGION_FIDELITY_GATE_V1,
    REPAIR_REGION_V1,
    ReviewerPrompt,
    render_formatting_prompt,
    render_gemma_audit_prompt,
    render_gemma_preference_prompt,
    render_qwen_audit_prompt,
    render_qwen_review_prompt,
    render_region_fidelity_gate_prompt,
    render_repair_prompt,
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
    bible_text: str = ""


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
            bible_text=self._config.bible_text,
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

    ``retry`` is the B4 JSON-resilience policy: an empty/truncated JSON body
    is retried (bounded, exponential backoff) by re-issuing the identical
    request — transport failures are never retried here (B4 §1/§3).

    ``bible_text`` is the B7 rendered book-memory section appended to the
    audit prompt so the model sees narrator gender, characters, facts, and
    address register when judging fidelity.
    """

    max_tokens: int = 16384
    template: ReviewerPrompt = QWEN_AUDIT_V1
    label: str = "phase3/qwen_chapter_audit"
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)
    bible_text: str = ""


class BackendQwenAuditEvaluator:
    """``pact_v4.phase3.audit.QwenAuditEvaluator`` over a ``CompletionBackend``.

    Transport-only role adapter (V4 A1 pattern): renders the Qwen audit
    prompt, sends it through ``backend.complete(request)``, and returns the
    raw assistant text. Issue parsing/validation and per-unit resumability
    live in ``pact_v4.phase3.audit``, unchanged. A transport failure raises
    ``CompletionError`` — ``run_chapter_audit`` converts any exception into a
    failed (resumable) unit, so the audit can never claim ``complete`` on a
    model failure.

    B4 (JSON resilience): an empty or truncated-JSON body is retried
    (``config.retry``, bounded + exponential backoff) by re-issuing the
    identical request — identity is unchanged by a retry. Transport failures
    are never retried here. When the budget is exhausted the last
    ``EmptyResponseError``/``TruncatedJSONError`` is re-raised, which
    ``run_chapter_audit`` records as a failed unit — never a semantic
    verdict.
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
            bible_text=self._config.bible_text,
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

        def _complete() -> str:
            # Re-issues the identical request on a retry: same prompt, same
            # model/backend, same request_options (none) — so retry never
            # changes cache/resume identity and never enables reasoning (B1).
            try:
                return self._backend.complete(request).text
            except CompletionError as exc:
                LOG.error("BackendQwenAuditEvaluator: backend failure: %s", exc)
                raise

        return retry_json_call(
            _complete, self._config.retry, label=self._config.label,
        )


@dataclass(frozen=True)
class BackendGemmaAuditEvaluatorConfig:
    max_tokens: int = 4096
    template: ReviewerPrompt = GEMMA_AUDIT_V1
    label: str = "phase3/gemma_russian_review"
    bible_text: str = ""


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
            bible_text=self._config.bible_text,
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


@dataclass(frozen=True)
class BackendRepairCallerConfig:
    """Phase 4A region-repair call settings.

    The repair output is a ``{"repaired": {pid: text}, "reason": ...}`` JSON
    object for the targeted PIDs. ``max_tokens`` is a floor with per-PID
    headroom (same Qwen fix as the fidelity gate) so a long repaired PID is
    not truncated mid-JSON — a truncation would otherwise surface as a
    spurious repair failure instead of a transport/format problem.

    ``retry`` is the B4 JSON-resilience policy: a truncated-JSON repair body
    is retried (bounded, exponential backoff) by re-issuing the identical
    request — transport failures are never retried here (B4 §2/§3).
    """

    max_tokens: int = 16384
    template: ReviewerPrompt = REPAIR_REGION_V1
    label: str = "phase4/region_repair"
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)


class BackendRepairCaller:
    """Phase 4A region/PID repair over a ``CompletionBackend``.

    Transport-only role adapter (V4 A1 pattern): renders the repair prompt
    (source + current translation + region + findings), sends it through
    ``backend.complete(request)``, and returns the raw assistant text.
    Output parsing/validation (strict JSON, PID-set enforcement) lives in
    ``pact_v4.phase4.repair`` — this class never invents a repair on its
    own. A transport failure raises ``CompletionError``; the repair layer
    converts it into a non-committed/debt outcome, never a semantic
    terminal status (rule "transport failure != semantic gate failure";
    no silent fallback).

    B4 (JSON resilience): a truncated-JSON repair body is retried
    (``config.retry``, bounded + exponential backoff) by re-issuing the
    identical request — identity is unchanged by a retry. Transport failures
    are never retried here. When the budget is exhausted the last
    ``EmptyResponseError``/``TruncatedJSONError`` is re-raised, which the
    repair layer converts into a non-committed/debt outcome — never a
    semantic verdict.
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendRepairCallerConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendRepairCallerConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(
        self,
        *,
        chunk_id: str,
        source: Mapping[str, str],
        translation: Mapping[str, str],
        region: Any,
        findings: Sequence[Mapping[str, str]],
    ) -> str:
        prompt = render_repair_prompt(
            chunk_id=chunk_id,
            source=dict(source),
            translation=dict(translation),
            region=region,
            findings=[dict(item) for item in findings],
            template=self._config.template,
        )
        dynamic_max_tokens = min(
            MAX_TOKENS_CEILING, self._max_tokens + TOKENS_PER_PID * len(translation),
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(self._backend, ("repair", "generator")),
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=dynamic_max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=self._config.label,
        )

        def _complete() -> str:
            # Re-issues the identical request on a retry: same prompt, same
            # model/backend, same request_options (none) — so retry never
            # changes cache/resume identity (B2 backend identity) and never
            # enables reasoning (B1).
            try:
                return self._backend.complete(request).text
            except CompletionError as exc:
                LOG.error("BackendRepairCaller: backend failure: %s", exc)
                raise

        return retry_json_call(
            _complete, self._config.retry, label=self._config.label,
        )


@dataclass(frozen=True)
class BackendRegionFidelityGateConfig:
    """L2b narrow Qwen re-gate call settings (``region_fidelity_gate``).

    The verdict output is the same short JSON object the full-chunk fidelity
    reviewer returns (parsed via ``_parse_qwen_verdict``), so narrow verdicts
    are directly comparable to full-chunk ones on a fixture. The input is a
    single PID + region, so the output budget is small; ``max_tokens`` stays
    a floor with a generous ceiling so a verbose ``reason`` is never
    truncated mid-JSON.
    """

    max_tokens: int = 4096
    template: ReviewerPrompt = REGION_FIDELITY_GATE_V1
    label: str = "phase4/region_fidelity_gate"


class BackendRegionFidelityGate:
    """``pact_v4.phase4.repair.RegionFidelityEvaluator`` over a
    ``CompletionBackend``.

    L2b (DECISIONS 2026-08-03): narrow per-region re-gate that renders only
    the edited PID's source + repaired text + region (``render_region_fidelity_gate_prompt``)
    and parses the verdict via ``_parse_qwen_verdict`` — the same parser the
    full-chunk fidelity gate uses. The model binding is the Qwen fidelity
    role (re-gate stays on Qwen; the editor stays on Gemma). A transport
    failure returns a failing ``GateResult`` (debt, never a semantic
    verdict); there is no silent fallback.
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendRegionFidelityGateConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendRegionFidelityGateConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(
        self, *, source_text: str, repaired_text: str, region: Any
    ) -> GateResult:
        prompt = render_region_fidelity_gate_prompt(
            source_text=source_text,
            repaired_text=repaired_text,
            region=region,
            template=self._config.template,
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(
                self._backend, ("fidelity_reviewer", "qwen_fidelity")
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
            LOG.error("BackendRegionFidelityGate: backend failure: %s", exc)
            return GateResult(
                gate="qwen_fidelity",
                passed=False,
                detail=f"qwen_fidelity: API failure: {exc}",
            )
        return _parse_qwen_verdict(response.text)


@dataclass(frozen=True)
class BackendFormattingCallerConfig:
    """Phase 5 span-mapping call settings.

    The formatting output is a ``{"mappings": [{"pid", "span_id",
    "target_text", "occurrence"}]}`` JSON object for one PID's unresolved
    spans. ``max_tokens`` is a floor with per-PID headroom (same Qwen fix as
    the fidelity gate) so a long PID with several spans is not truncated
    mid-JSON — a truncation would otherwise surface as a spurious incident
    instead of a transport/format problem.
    """

    max_tokens: int = 8192
    template: ReviewerPrompt = FORMAT_SPANS_V1
    label: str = "phase5/formatting_align"


class BackendFormattingCaller:
    """Phase 5 §8.14 span-mapping model fallback over a ``CompletionBackend``.

    Transport-only role adapter (V4 A1 pattern): renders the formatting
    prompt (source text + unresolved source spans + Russian translation),
    sends it through ``backend.complete(request)``, and returns the raw
    assistant text. Output parsing/validation (strict JSON, PID/span-set
    enforcement, substring verification) lives in
    ``pact_v4.phase5.formatting`` — this class never invents a mapping on its
    own. A transport failure raises ``CompletionError``; the formatting
    module converts it into a blocking incident recorded as debt, never a
    semantic terminal status (rule "transport failure != semantic gate
    failure"; no silent fallback).
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendFormattingCallerConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendFormattingCallerConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(
        self,
        *,
        pid: str,
        source_text: str,
        translation: str,
        spans: Sequence[Mapping[str, Any]],
    ) -> str:
        prompt = render_formatting_prompt(
            pid=pid,
            source_text=source_text,
            translation=translation,
            spans=[dict(item) for item in spans],
            template=self._config.template,
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(
                self._backend, ("formatting", "repair", "generator")
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
            LOG.error("BackendFormattingCaller: backend failure: %s", exc)
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
    "BackendRepairCallerConfig",
    "BackendRepairCaller",
    "BackendRegionFidelityGateConfig",
    "BackendRegionFidelityGate",
    "BackendFormattingCallerConfig",
    "BackendFormattingCaller",
]
