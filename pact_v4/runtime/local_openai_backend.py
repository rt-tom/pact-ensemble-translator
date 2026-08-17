"""``LocalOpenAIBackend`` — the local ``llama-server`` transport adapter.

A thin ``CompletionBackend`` implementation over the existing
``ApiClient`` (``pact_v4.runtime.api_client``). It:

* builds the chat payload from a ``CompletionRequest`` and executes
  ``ApiClient.complete(...)``;
* preserves the existing behaviour exactly: ``response_format=json_object``
  when a ``response_schema`` is requested, the Gemma ``peg-gemma4``
  grammar-reject fallback, and bounded transient-error retries — all of
  which stay inside ``ApiClient``;
* normalizes the result (text, usage, finish_reason) into a
  ``CompletionResponse`` and records a ``BackendCallRecord`` per call;
* exposes a ``BackendDescriptor`` whose ``identity_hash`` includes the
  ``ApiClientConfig`` settings that can change the model answer (sampling,
  context size, retry policy) and excludes local transport details;
* ``close()`` is idempotent, closes the underlying HTTP session, and never
  stops a foreign ``llama-server`` — process lifecycle stays with the
  lifecycle adapter that started the server.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from pact_v4.runtime.api_client import ApiClient, ApiClientConfig, ApiClientError
from pact_v4.runtime.backend_protocol import (
    KIND_LOCAL_LLAMA,
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
    ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS,
)

LOG = logging.getLogger(__name__)


# Adapter/transport version of this OpenAI-compatible chat-completions
# adapter. Bump when the request/response contract changes.
LOCAL_OPENAI_TRANSPORT_VERSION = "openai-chat-completions/v1"


@dataclass(frozen=True)
class LocalOpenAIBackendConfig:
    """Identity-relevant settings of a ``LocalOpenAIBackend``.

    These mirror the fields of ``ApiClientConfig`` that can change the
    model answer (and therefore belong in ``BackendDescriptor`` identity).
    """

    api: ApiClientConfig = field(default_factory=ApiClientConfig)
    name: str = "local-openai"
    model_bindings: Mapping[str, str] = field(default_factory=dict)
    transport_version: str = LOCAL_OPENAI_TRANSPORT_VERSION
    endpoint_family: str = ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_bindings", dict(self.model_bindings))


def _build_descriptor(cfg: LocalOpenAIBackendConfig) -> BackendDescriptor:
    api = cfg.api
    bindings = dict(cfg.model_bindings) or {"default": api.model}
    effective_options = {
        "temperature": api.temperature,
        "top_p": api.top_p,
        "top_k": api.top_k,
        "context_size": api.context_size,
        "timeout_seconds": api.timeout_seconds,
        "http_retries": api.http_retries,
        "retry_delay_seconds": api.retry_delay_seconds,
        "structured_output": {
            "mode": "json_object",
            "schema_version": "pact-json-object/v1",
        },
    }
    return BackendDescriptor(
        kind=KIND_LOCAL_LLAMA,
        transport_version=cfg.transport_version,
        endpoint_family=cfg.endpoint_family,
        public_endpoint=api.chat_url,
        model_bindings=bindings,
        effective_options=effective_options,
    )


class LocalOpenAIBackend:
    """``CompletionBackend`` adapter over ``ApiClient``."""

    def __init__(
        self,
        *,
        api: Optional[ApiClient] = None,
        config: Optional[LocalOpenAIBackendConfig] = None,
    ) -> None:
        if api is None and config is None:
            config = LocalOpenAIBackendConfig()
        if api is None and config is not None:
            api = ApiClient(config.api, name=config.name)
        assert api is not None  # narrowed by the two branches above
        self._api = api
        self._cfg = config or LocalOpenAIBackendConfig(
            api=api.config, name=api.name,
            model_bindings={"default": api.config.model},
        )
        self._records: list[BackendCallRecord] = []
        self._closed = False
        # MONITOR-V2 (2.1): optional per-call usage sink
        # (``UsageRecordWriter.write_call``), attached by the coordinator's
        # ``set_usage_writer`` so LOCAL llama-server calls also land in
        # ``usage.ndjson``. Mirror of ``OpenCodeServerBackend``.
        self._usage_sink: Optional[Any] = None

    # ------------------------------------------------------------------
    # CompletionBackend protocol
    # ------------------------------------------------------------------

    @property
    def descriptor(self) -> BackendDescriptor:
        return _build_descriptor(self._cfg)

    @property
    def api(self) -> ApiClient:
        return self._api

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._closed:
            raise CompletionError(
                f"{self._cfg.name}: backend is closed; cannot complete a request"
            )
        if request.request_options:
            # The local adapter applies request fields directly through
            # ApiClient.complete and cannot silently honour transport
            # options that would change the model answer.
            if "reasoning" in request.request_options:
                # V4.1 A2: the local generator receives its reasoning budget
                # from the SERVER ARGS (--reasoning-budget, see plan §3.4),
                # never from request_options — the OpenCode backend is the
                # only transport that maps request_options reasoning to
                # 'reasoningEffort'. The local llama-server transport has no
                # such per-request field — refuse loudly with a reason
                # instead of an opaque transport failure. The pipeline local
                # path never emits reasoning request_options; this branch
                # guards direct library-level use.
                raise CompletionError(
                    f"{self._cfg.name}: request option 'reasoning' is only "
                    f"supported by the OpenCode backend (opencode serve "
                    f"'reasoningEffort'); the local llama-server transport "
                    f"cannot express a reasoning effort. Use --reasoning 0 "
                    f"with a local backend."
                )
            raise CompletionError(
                f"{self._cfg.name}: unsupported request option(s) "
                f"{sorted(request.request_options)} for LocalOpenAIBackend"
            )
        actual_model = self._api.config.model
        if request.model_ref != actual_model:
            # The request claims a role→model binding that this backend is
            # not actually serving. Refuse rather than silently call a
            # different model (plan: no silent fallback). ``model_ref`` is
            # guaranteed non-empty by ``CompletionRequest`` validation.
            raise CompletionError(
                f"{self._cfg.name}: request model_ref {request.model_ref!r} does not "
                f"match the backend's actual model {actual_model!r}"
            )
        messages = [{"role": msg.role, "content": msg.content} for msg in request.messages]
        started = time.perf_counter()
        try:
            text = self._api.complete(
                messages,
                max_tokens=request.max_output_tokens,
                temperature=request.temperature,
                response_format_json=request.response_schema is not None,
                label=request.label,
                on_reasoning_chunk=request.on_reasoning_chunk,
            )
        except ApiClientError as exc:
            LOG.error("%s: %s API failure: %s", self._cfg.name, self._api.name, exc)
            raise CompletionError(
                f"{self._cfg.name}: {self._api.name} API failure: {exc}"
            ) from exc
        finally:
            wall = time.perf_counter() - started

        # ``ApiClient`` records each call synchronously; injected test stubs
        # may not expose ``call_records``, so fall back to defaults.
        calls = getattr(self._api, "call_records", None) or []
        record = calls[-1] if calls else None
        attempts = getattr(record, "attempt_count", None) if record else None
        retry_count = max(0, int(attempts) - 1) if attempts else 0
        streamed = bool(getattr(record, "streamed", False)) if record else False
        usage = dict(record.usage) if record else {}
        # MONITOR-V2 (2.1): llama-server reports OpenAI-style
        # prompt_tokens/completion_tokens; normalize to the usage-record
        # vocabulary (input_tokens/output_tokens) so local rows carry token
        # counts in usage.ndjson exactly like remote rows. Only values the
        # server actually reported are mapped (never invented).
        if "input_tokens" not in usage and "prompt_tokens" in usage:
            usage["input_tokens"] = usage["prompt_tokens"]
        if "output_tokens" not in usage and "completion_tokens" in usage:
            usage["output_tokens"] = usage["completion_tokens"]
        response = CompletionResponse(
            text=text,
            structured=None,
            provider="local_llama",
            model=actual_model,
            finish_reason=record.finish_reason if record else None,
            usage=usage,
            wall_seconds=round(wall, 3),
            request_id=None,
            session_id=None,
            retry_count=retry_count,
            raw_metadata={
                "http_status": record.http_status if record else None,
                "response_format_attempted": (
                    record.response_format_attempted if record else None
                ),
                "attempt_count": attempts,
                "request_options": dict(request.request_options),
                "reasoning": record.reasoning if record else "",
                # REASONING-STREAM provenance: True when the call used the
                # SSE streaming transport (live reasoning chunks); False for
                # the batch path (reasoning delivered after completion).
                "reasoning_streamed": streamed,
            },
        )
        self._records.append(
            BackendCallRecord(
                label=request.label,
                model_ref=request.model_ref,
                request_id=None,
                session_id=None,
                retry_count=retry_count,
                finish_reason=response.finish_reason,
                usage=response.usage,
                wall_seconds=response.wall_seconds,
                raw_metadata=response.raw_metadata,
            )
        )
        # MONITOR-V2 (2.1): per-call usage write at completion (crash-safe:
        # the call is in usage.ndjson the moment it finishes, not at a phase
        # boundary). Mirror of ``OpenCodeServerBackend`` — the local backend
        # has no failure record (transport errors raise before a record is
        # created), so the success path is the only emit point.
        self._emit_usage(self._records[-1])
        return response

    def set_usage_sink(self, sink: Any) -> None:
        """Attach a per-call usage sink (``UsageRecordWriter.write_call``).

        Called by the coordinator's ``set_usage_writer`` for local
        sub-backends so every completed local llama-server call is appended
        to ``usage.ndjson`` at the moment it finishes (mirror of
        ``OpenCodeServerBackend.set_usage_sink``).
        """
        self._usage_sink = sink

    def _emit_usage(self, record: BackendCallRecord) -> None:
        if self._usage_sink is not None:
            try:
                self._usage_sink(record)
            except Exception:  # noqa: BLE001 -- usage is diagnostics, never a gate
                LOG.warning(
                    "LocalOpenAIBackend: usage sink failed; disabling",
                    exc_info=True,
                )
                self._usage_sink = None

    def close(self) -> None:
        # Idempotent: closes the HTTP session this backend owns, but never
        # stops a llama-server process. Lifecycle is the job of the
        # lifecycle adapter that launched the server.
        if self._closed:
            return
        try:
            self._api._session.close()
        except Exception as exc:  # pragma: no cover - defensive teardown
            LOG.warning("%s: error closing HTTP session: %s", self._cfg.name, exc)
        self._closed = True

    def call_records(self) -> Sequence[BackendCallRecord]:
        return list(self._records)


__all__ = [
    "LOCAL_OPENAI_TRANSPORT_VERSION",
    "LocalOpenAIBackendConfig",
    "LocalOpenAIBackend",
]
