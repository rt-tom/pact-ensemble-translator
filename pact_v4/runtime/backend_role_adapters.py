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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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
    GEMMA_AUDIT_V1,
    GEMMA_RUSSIAN_PREFERENCE_V1,
    QWEN_AUDIT_V1,
    QWEN_FIDELITY_V1,
    REGION_FIDELITY_GATE_BATCH_V1,
    REGION_FIDELITY_GATE_V1,
    REPAIR_REGION_V1,
    ReviewerPrompt,
    render_gemma_audit_prompt,
    render_gemma_preference_prompt,
    render_qwen_audit_prompt,
    render_qwen_review_prompt,
    render_region_fidelity_gate_batch_prompt,
    render_region_fidelity_gate_prompt,
    render_repair_prompt,
)
from pact_v4.runtime.qwen_evaluator import (
    MAX_TOKENS_CEILING,
    TOKENS_PER_PID,
    _parse_qwen_verdict,
    _parse_qwen_verdicts,
)

LOG = logging.getLogger(__name__)


# Phase 2B generation calls produce JSON-object output. The output budget is
# 70000 tokens (V4.1 A1, owner decision 2026-08-08; raised 2026-08-19 from
# 32768 so high-reasoning remotes like Muse keep room for content after
# reasoning tokens): whole-chapter generation
# emits the full chapter in one call (chapter 0001 ~12-19k tokens, the longest
# chapter 0077 ~21k). For chunked calls the bound is still generous; the
# OpenCode transport does not send max_output_tokens in the POST body (Gate 0
# §2.4), so this value lives in the request/identity, not the transport wire.
# Qwen-role budgets stay capped by MAX_TOKENS_CEILING (untouched).
DEFAULT_MAX_TOKENS = 70000


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


def _reasoning_transported_via_request_options(
    backend: CompletionBackend, model_ref: str
) -> bool:
    """Whether a reasoning budget for ``model_ref`` travels via request_options.

    V4.1 A2 (plan §0.1/§3.4): the OpenCode backend maps request_options
    reasoning to ``reasoningEffort`` — the request-level transport. Local
    llama-server transports (``LocalOpenAIBackend``/``LocalRoutingBackend``
    and composite profiles whose generator routes to a local sub-backend)
    receive the reasoning budget from the SERVER ARGS
    (``--reasoning-budget``), and ``LocalOpenAIBackend`` rejects any
    request_options as a library-level guard — so the request to a local
    generator must never carry reasoning request_options.

    Composite backends are resolved down to the sub-backend that actually
    serves ``model_ref``, so the decision follows the concrete transport.
    """
    from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend

    if isinstance(backend, LocalOpenAIBackend):
        return False
    # LocalRoutingBackend / CompositeCompletionBackend live in runtime_config;
    # imported lazily to keep the module-import graph acyclic (runtime_config
    # imports this module inside build_role_adapters).
    from pact_v4.runtime.runtime_config import (
        CompositeCompletionBackend,
        LocalRoutingBackend,
    )

    if isinstance(backend, LocalRoutingBackend):
        return False
    if isinstance(backend, CompositeCompletionBackend):
        serving = backend.serving_backend(model_ref)
        if serving is None:
            # Unknown routing: fall back to request_options transport so the
            # behaviour matches the historical path for unrecognised backends.
            return True
        return _reasoning_transported_via_request_options(serving, model_ref)
    return True


@dataclass(frozen=True)
class BackendModelCallerConfig:
    """Phase 2B generation call settings.

    ``retry`` is the B4 JSON-resilience policy (B10: extended to the
    generation adapter): an empty/truncated JSON body is retried (bounded,
    exponential backoff) by re-issuing the identical request — transport
    failures are never retried here (B4 §1/§3). ``max_tokens`` is the
    generation output budget (chunk-sized, see ``DEFAULT_MAX_TOKENS``).
    """

    max_tokens: int = DEFAULT_MAX_TOKENS
    label: str = "phase2b-generation"
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)


class BackendModelCaller:
    """``ModelCaller`` protocol implementation over a ``CompletionBackend``.

    Renders the bundle into a single user message, sends it through the
    backend, and returns the raw assistant text. JSON validation, PID-set
    enforcement, and cache identity all live in ``pact_v4.phase2.generation``
    — this class does not duplicate any of that.

    B4 (JSON resilience, B10): an empty or truncated-JSON generation body
    (the run_002 ``incomplete_generation`` failure mode) is retried
    (``config.retry``, bounded + exponential backoff) by re-issuing the
    identical request — identity is unchanged by a retry. Transport failures
    are never retried here. When the budget is exhausted the last
    ``EmptyResponseError``/``TruncatedJSONError`` is re-raised — the
    generation layer records it as a failed candidate, never a semantic
    verdict.
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendModelCallerConfig] = None,
        on_reasoning_chunk: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendModelCallerConfig()
        self._max_tokens = int(self._config.max_tokens)
        # V4.1 GEN-REASONING: the reasoning text of the most recent backend
        # completion (raw_metadata['reasoning'], '' when the transport or
        # provider reported none). Exposed for the whole-chapter generation
        # layer to persist per-attempt reasoning diagnostics — never part of
        # cache/resume identity.
        self._last_reasoning: str = ""
        # V4.1 GEN-STREAM: optional live reasoning sink forwarded into every
        # CompletionRequest this caller issues (see set_reasoning_chunk_sink).
        # Diagnostics-only — never part of cache/resume identity.
        self._on_reasoning_chunk: Optional[Callable[[str], None]] = on_reasoning_chunk

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    @property
    def last_reasoning(self) -> str:
        """Reasoning text of the most recent backend completion ('' when none)."""
        return self._last_reasoning

    @property
    def last_raw(self) -> str:
        """Raw text of the most recent backend completion ('' when none).

        RAW-SINK (architect, run_remote_006): captured in ``_complete``
        BEFORE ``retry_json_call`` classifies the body, so the raw survives
        a TruncatedJSONError — the disk trail for whole-chapter diagnosis.
        """
        return self._last_raw

    def reset_attempt_state(self) -> None:
        """Clear the per-attempt reasoning diagnostic at a call-attempt boundary.

        V4.1 GEN-REASONING (RV t_a790dbab): the lifecycle wrappers invoke
        this BEFORE model acquisition (``ensure_resident``) so that an
        acquisition failure (``CompletionError`` from a model load/swap)
        never exposes the previous successful completion's reasoning — the
        wrapped ``__call__`` is never entered in that case, so its
        clear-at-start reset cannot run. Direct callers keep the existing
        clear-at-start behavior inside ``__call__``.
        """
        self._last_reasoning = ""
        self._last_raw = ""

    def set_reasoning_chunk_sink(
        self, sink: Optional[Callable[[str], None]]
    ) -> None:
        """Install (or clear, with ``None``) the live reasoning-chunk sink.

        V4.1 GEN-STREAM: the whole-chapter generation layer opens the
        per-attempt reasoning file BEFORE the model call (via
        ``open_reasoning_writer``, the REASONING-STREAM pattern) and passes
        the returned appender here so ``__call__`` forwards it into the
        ``CompletionRequest.on_reasoning_chunk`` hook — the file then grows
        live while the model is still generating. Diagnostics-only: never
        part of cache/resume identity, and a failing sink never breaks the
        model call (best-effort, like every other reasoning hook).
        """
        self._on_reasoning_chunk = sink

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
        model_ref = _model_ref_for(self._backend, (bundle.role, "generator"))
        request_options: Dict[str, Any] = {}
        if bundle.params.reasoning and _reasoning_transported_via_request_options(
            self._backend, model_ref
        ):
            # V4.1: Phase 2B generation reasoning budget (0=off, 1=low,
            # 2=medium, 3=high). Transported via request_options so the
            # opencode backend can map it to the top-level ``reasoningEffort``
            # field; 0/absent keeps the historical B1 baseline (no field).
            # Only the generation caller carries it — the Qwen audit / repair
            # / formatting adapters never set request_options. V4.1 A2: local
            # llama-server transports receive the reasoning budget from their
            # server args (--reasoning-budget), so the request to them must
            # NOT carry request_options reasoning (LocalOpenAIBackend rejects
            # it — a library-level guard, plan §3.4/§0.1).
            request_options["reasoning"] = bundle.params.reasoning
        request = CompletionRequest(
            model_ref=model_ref,
            messages=(Message(role="user", content=user_text),),
            max_output_tokens=self._max_tokens,
            temperature=bundle.params.temperature,
            response_schema=JSON_OBJECT_SCHEMA,
            label=f"phase2b/{bundle.role}/{bundle.chunk_id}",
            request_options=request_options,
            # AF (2026-08-10): serve 1.4.7 applied a default ~32k output
            # budget to message bodies that carry system/tools (agentic
            # mode), truncating whole-chapter reasoning at 32000 tokens
            # (finish=length, empty output — 2/3 remote whole-chapter
            # attempts). The neutral system prompt and the all-disabled
            # tools map do not change the model answer, so the generation
            # request omits both — the verbatim Gate 0 body
            # (model+parts+reasoningEffort) that measured 55915 reasoning
            # tokens with finish=stop. Generation-only: the Qwen audit /
            # repair / formatting adapters keep the historical
            # system+tools body. Inert for local llama-server transports
            # (they never read the field).
            omit_system_tools=True,
            # V4.1 GEN-STREAM: forward the live reasoning sink (installed by
            # the generation layer per attempt via set_reasoning_chunk_sink)
            # so the transport can grow the per-attempt *_reasoning.txt file
            # while the model is still generating (REASONING-STREAM pattern).
            # Diagnostics-only: a None sink keeps the historical behavior.
            on_reasoning_chunk=self._on_reasoning_chunk,
        )

        # V4.1 GEN-REASONING: each model-call attempt starts with a clean
        # reasoning slate. On a transport failure no response ever arrives,
        # so last_reasoning stays '' (never a stale value from a previous
        # attempt); on a completed backend call _complete() records the
        # reasoning text from raw_metadata.
        self._last_reasoning = ""
        # RAW-SINK (architect, run_remote_006): capture the raw model text of
        # this attempt BEFORE retry_json_call classifies it — the transport
        # returned the text, but classify_response_text may raise
        # TruncatedJSONError/EmptyResponseError, and then the raw would
        # otherwise vanish (no disk trail for diagnosis). _complete()
        # overwrites it per backend call; a transport failure leaves it ''.
        self._last_raw = ""

        def _complete() -> str:
            # Re-issues the identical request on a retry: same prompt, same
            # model/backend, same request_options (including any pinned
            # reasoning) — so retry never changes cache/resume identity and
            # never switches the reasoning budget mid-request (B1).
            try:
                response = self._backend.complete(request)
            except CompletionError as exc:
                LOG.error("BackendModelCaller: backend failure: %s", exc)
                raise
            # GEN-REASONING: capture the reasoning text of THIS completion
            # (open-code backend: type=reasoning parts; local: the server's
            # reported reasoning field). Diagnostics only — never part of
            # the returned text, cache, or resume identity.
            metadata = response.raw_metadata if isinstance(response.raw_metadata, Mapping) else {}
            reasoning = metadata.get("reasoning")
            self._last_reasoning = str(reasoning) if reasoning is not None else ""
            # RAW-SINK: keep the raw text even if the caller's classify step
            # (retry_json_call → classify_response_text) later rejects it —
            # the disk trail must survive TruncatedJSONError.
            self._last_raw = response.text or ""
            return response.text

        return retry_json_call(
            _complete, self._config.retry, label=request.label,
        )


@dataclass(frozen=True)
class BackendQwenEvaluatorConfig:
    """Phase 2C Qwen fidelity-gate call settings.

    ``retry`` is the B4 JSON-resilience policy (B10: extended to the
    fidelity gate): an empty/truncated JSON verdict body is retried
    (bounded, exponential backoff) by re-issuing the identical request —
    transport failures are never retried here (B4 §1/§3) and still surface
    as a failing ``GateResult``.
    """

    max_tokens: int = 16384
    template: ReviewerPrompt = QWEN_FIDELITY_V1
    bible_text: str = ""
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)


class BackendQwenEvaluator:
    """``QwenEvaluator`` protocol implementation over a ``CompletionBackend``.

    B4 (JSON resilience, B10): an empty or truncated-JSON verdict body is
    retried (``config.retry``, bounded + exponential backoff) by re-issuing
    the identical request — identity is unchanged by a retry. Transport
    failures are never retried here and keep returning a failing
    ``GateResult`` (the cascade's failed-gate contract). When the retry
    budget is exhausted the last ``EmptyResponseError``/``TruncatedJSONError``
    is re-raised — never a semantic verdict.
    """

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

        def _complete() -> str:
            # Re-issues the identical request on a retry: same prompt, same
            # model/backend, same request_options (none) — so retry never
            # changes cache/resume identity and never enables reasoning (B1).
            try:
                return self._backend.complete(request).text
            except CompletionError as exc:
                LOG.error("BackendQwenEvaluator: backend failure: %s", exc)
                raise

        try:
            raw = retry_json_call(
                _complete, self._config.retry, label="phase2c/qwen_fidelity",
            )
        except CompletionError as exc:
            return GateResult(
                gate="qwen_fidelity",
                passed=False,
                detail=f"qwen_fidelity: API failure: {exc}",
            )
        return _parse_qwen_verdict(raw)


@dataclass(frozen=True)
class BackendGemmaSelectorConfig:
    """Phase 2C Gemma Russian-preference call settings.

    ``retry`` is the B4 JSON-resilience policy (B10: extended to the
    selector): an empty/truncated JSON verdict body is retried (bounded,
    exponential backoff) by re-issuing the identical request — transport
    failures are never retried here (B4 §1/§3) and still surface as a
    failing ``GateResult``.
    """

    max_tokens: int = 1024
    template: ReviewerPrompt = GEMMA_RUSSIAN_PREFERENCE_V1
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)


class BackendGemmaSelector:
    """``GemmaSelector`` protocol implementation over a ``CompletionBackend``.

    B4 (JSON resilience, B10): an empty or truncated-JSON verdict body is
    retried (``config.retry``, bounded + exponential backoff) by re-issuing
    the identical request — identity is unchanged by a retry. Transport
    failures are never retried here and keep returning a failing
    ``GateResult`` (the cascade's failed-gate contract). When the retry
    budget is exhausted the last ``EmptyResponseError``/``TruncatedJSONError``
    is re-raised — never a semantic verdict.
    """

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

        def _complete() -> str:
            # Re-issues the identical request on a retry: same prompt, same
            # model/backend, same request_options (none) — so retry never
            # changes cache/resume identity and never enables reasoning (B1).
            try:
                return self._backend.complete(request).text
            except CompletionError as exc:
                LOG.error("BackendGemmaSelector: backend failure: %s", exc)
                raise

        try:
            raw = retry_json_call(
                _complete, self._config.retry,
                label="phase2c/gemma_russian_preference",
            )
        except CompletionError as exc:
            return GateResult(
                gate="gemma_russian_preference",
                passed=False,
                detail=f"gemma_russian_preference: API failure: {exc}",
            )
        return _parse_gemma_preference(raw, valid_candidate_ids=valid_ids)


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
    """Step 6 Gemma audit call settings.

    ``retry`` is the B4 JSON-resilience policy (B10: extended to the Gemma
    audit adapter): an empty/truncated JSON body is retried (bounded,
    exponential backoff) by re-issuing the identical request — transport
    failures are never retried here (B4 §1/§3) and still raise
    ``CompletionError`` for ``run_chapter_audit`` to record as a failed unit.
    """

    max_tokens: int = 4096
    template: ReviewerPrompt = GEMMA_AUDIT_V1
    label: str = "phase3/gemma_russian_review"
    bible_text: str = ""
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)


class BackendGemmaAuditEvaluator:
    """``pact_v4.phase3.audit.GemmaAuditEvaluator`` over a ``CompletionBackend``.

    Russian-only review: never given the English source (spec
    "Russian-only review без оригинала"), matching the protocol signature
    exactly. Transport failures raise ``CompletionError`` for
    ``run_chapter_audit`` to record as a failed unit.

    B4 (JSON resilience, B10): an empty or truncated-JSON body is retried
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

        def _complete() -> str:
            # Re-issues the identical request on a retry: same prompt, same
            # model/backend, same request_options (none) — so retry never
            # changes cache/resume identity and never enables reasoning (B1).
            try:
                return self._backend.complete(request).text
            except CompletionError as exc:
                LOG.error("BackendGemmaAuditEvaluator: backend failure: %s", exc)
                raise

        return retry_json_call(
            _complete, self._config.retry, label=self._config.label,
        )


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

    ``template`` drives the single-region prompt (``__call__``);
    ``batch_template`` drives the batched prompt (``batch``) and defaults to
    the dedicated batch contract (``REGION_FIDELITY_GATE_BATCH_V1`` — the
    ``{\"verdicts\": [...]}`` array schema the batched parser
    ``_parse_qwen_verdicts`` requires). B12-F1: the batch path must never
    inherit the single-region template, whose instructions ask for one
    flat verdict object the batch parser would fail closed on.
    """

    max_tokens: int = 4096
    template: ReviewerPrompt = REGION_FIDELITY_GATE_V1
    batch_template: ReviewerPrompt = REGION_FIDELITY_GATE_BATCH_V1
    label: str = "phase4/region_fidelity_gate"
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)


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

    B4 (JSON resilience, B10): an empty or truncated-JSON verdict body is
    retried (``config.retry``, bounded + exponential backoff) by re-issuing
    the identical request — identity is unchanged by a retry. Transport
    failures are never retried here and keep returning a failing
    ``GateResult``. When the retry budget is exhausted the last
    ``EmptyResponseError``/``TruncatedJSONError`` is re-raised — never a
    semantic verdict.
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

        def _complete() -> str:
            # Re-issues the identical request on a retry: same prompt, same
            # model/backend, same request_options (none) — so retry never
            # changes cache/resume identity and never enables reasoning (B1).
            try:
                return self._backend.complete(request).text
            except CompletionError as exc:
                LOG.error("BackendRegionFidelityGate: backend failure: %s", exc)
                raise

        try:
            raw = retry_json_call(
                _complete, self._config.retry, label=self._config.label,
            )
        except CompletionError as exc:
            return GateResult(
                gate="qwen_fidelity",
                passed=False,
                detail=f"qwen_fidelity: API failure: {exc}",
            )
        return _parse_qwen_verdict(raw)

    def batch(
        self,
        items: Sequence[Mapping[str, Any]],
    ) -> Sequence[GateResult]:
        """Re-gate several regions of the same chunk in one call (B12).

        ``items`` is a list of ``{source_text, repaired_text, region}``
        payloads; the batched prompt renders one ``REGION <index>:`` block
        per item and the response is
        ``{"verdicts": [{...verdict...}, ...]}`` — one verdict object per
        region, in order. Each verdict is parsed by the same
        ``_parse_qwen_verdict`` as the single-region re-gate, so a batched
        verdict is directly comparable to a narrow one on a fixture. A
        transport failure yields one failing ``GateResult`` per region
        (debt, never a semantic verdict), mirroring the single-region path.

        v41-runtime-efficiency 3.1: per-region tokens are 4096; the batch
        ceiling is ``MAX_TOKENS_CEILING`` (24576) so large batches are
        chunked into ``ceiling//4096``-sized groups and the output budget
        is ``min(ceiling, 4096*len)``. Chunk count is deterministic from
        ``MAX_TOKENS_CEILING``.
        """
        if not items:
            return ()
        # v41 3.1: fixed 4096 output-token unit per item (approved spec);
        # independent of config.max_tokens. Batch size is ceiling//4096,
        # budget is min(ceiling, 4096*len(chunk)).
        chunk_size = max(1, MAX_TOKENS_CEILING // 4096)
        results: List[GateResult] = []
        for start in range(0, len(items), chunk_size):
            chunk = list(items[start:start + chunk_size])
            prompt = render_region_fidelity_gate_batch_prompt(
                items=[dict(item) for item in chunk],
                template=self._config.batch_template,
            )
            max_tokens = min(MAX_TOKENS_CEILING, 4096 * len(chunk))
            request = CompletionRequest(
                model_ref=_model_ref_for(
                    self._backend, ("fidelity_reviewer", "qwen_fidelity")
                ),
                messages=(Message(role="user", content=prompt),),
                max_output_tokens=max_tokens,
                temperature=0.0,
                response_schema=JSON_OBJECT_SCHEMA,
                label=self._config.label,
            )

            def _complete(req: CompletionRequest = request) -> str:  # type: ignore[no-redef]
                try:
                    return self._backend.complete(req).text
                except CompletionError as exc:
                    LOG.error("BackendRegionFidelityGate.batch: backend failure: %s", exc)
                    raise

            try:
                raw = retry_json_call(
                    _complete, self._config.retry, label=self._config.label,
                )
            except CompletionError as exc:
                chunk_results = tuple(
                    GateResult(
                        gate="qwen_fidelity",
                        passed=False,
                        detail=f"qwen_fidelity: API failure: {exc}",
                    )
                    for _ in chunk
                )
                results.extend(chunk_results)
                continue
            results.extend(list(_parse_qwen_verdicts(raw, count=len(chunk))))
        return tuple(results)


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
]
