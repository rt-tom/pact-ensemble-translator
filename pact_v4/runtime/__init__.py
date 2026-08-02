"""Real HTTP adapters wiring the Pact v4 library interfaces to a live
``llama-server`` (or any OpenAI-compatible chat-completions endpoint).

This package is intentionally isolated from ``pact_v4.phase2`` so the library
modules stay free of network/codex-side code. The Phase-2B ``ModelCaller``
and Phase-2C ``QwenEvaluator``/``GemmaSelector`` are abstract ``Protocol``s
by design; this package supplies one production-flavoured implementation of
each that is wired to a real HTTP client.

These adapters are kept thin and explicit:

* No global configuration object — every component receives its own config
  (chat_url, model, max_tokens, temperature, top_p, top_k) so the same
  library code can be pointed at Qwen and Gemma (and future models) without
  cross-talk.
* No caching beyond the per-chapter ``GenerationCache`` the library already
  owns: this layer is dumb and stateless apart from connection reuse.
* No model-side reasoning budget: Phase 2B forces ``reasoning == 0`` at the
  library level; we propagate that here by *not* emitting any
  ``chat_template_kwargs.enable_thinking`` flag unless the caller asks for
  it explicitly.
"""

from __future__ import annotations

from pact_v4.runtime.backend_protocol import (
    ALLOWED_REQUEST_OPTIONS,
    ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS,
    JSON_OBJECT_SCHEMA,
    KIND_COMPOSITE,
    KIND_LOCAL_LLAMA,
    KIND_OPENCODE_SERVER,
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaSelector,
    BackendModelCaller,
    BackendQwenEvaluator,
)
from pact_v4.runtime.local_openai_backend import (
    LocalOpenAIBackend,
    LocalOpenAIBackendConfig,
)
from pact_v4.runtime.opencode_backend import (
    DEFAULT_DISABLED_TOOLS,
    DEFAULT_SYSTEM_PROMPT,
    ENDPOINT_FAMILY_OPENCODE_HTTP,
    ERROR_INVALID_MODEL_OUTPUT,
    ERROR_PROVIDER_429,
    ERROR_PROVIDER_5XX,
    ERROR_PROVIDER_AUTH,
    ERROR_PROVIDER_MODEL_UNAVAILABLE,
    ERROR_REMOTE_BUDGET_EXHAUSTED,
    ERROR_REQUEST_NOT_SUPPORTED,
    ERROR_SEMANTIC_GATE_FAILED,
    ERROR_SERVER_VERSION_UNSUPPORTED,
    ERROR_STRUCTURED_OUTPUT_FAILED,
    ERROR_TRANSPORT_NETWORK,
    ERROR_TRANSPORT_TIMEOUT,
    OPENCODE_PINNED_SERVER_VERSION,
    OPENCODE_SERVER_TRANSPORT_VERSION,
    OPENCODE_SYSTEM_PROMPT_VERSION,
    OpenCodeError,
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
    RemoteBudget,
    build_opencode_descriptor,
)
from pact_v4.runtime.opencode_server_lifecycle import (
    DEFAULT_HOSTNAME,
    DEFAULT_USERNAME,
    ManagedServerError,
    ManagedServerSpec,
    OpenCodeServerProcess,
)
from pact_v4.runtime.runtime_config import (
    BackendRuntimeConfig,
    CompositeBackendConfig,
    CompositeCompletionBackend,
    LocalLlamaBackendConfig,
    LocalRoutingBackend,
    OpenCodeBackendConfig,
    StrictBackendConfig,
    build_role_adapters,
    build_role_backend,
    load_runtime_config,
)
from pact_v4.runtime.runtime_coordinator import (
    EVENT_KIND_LOCAL_SWITCH,
    EVENT_KIND_REMOTE_CALL,
    BackendEvent,
    CompositeRuntimeCoordinator,
    LocalLifecycleCoordinator,
    RemoteRuntimeCoordinator,
    RuntimeCoordinator,
    local_lifecycle_summary,
    remote_calls_summary,
)

__all__ = [
    "ALLOWED_REQUEST_OPTIONS",
    "ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS",
    "JSON_OBJECT_SCHEMA",
    "KIND_COMPOSITE",
    "KIND_LOCAL_LLAMA",
    "KIND_OPENCODE_SERVER",
    "BackendCallRecord",
    "BackendDescriptor",
    "BackendGemmaSelector",
    "BackendModelCaller",
    "BackendQwenEvaluator",
    "CompletionBackend",
    "CompletionError",
    "CompletionRequest",
    "CompletionResponse",
    "LocalOpenAIBackend",
    "LocalOpenAIBackendConfig",
    "Message",
    "DEFAULT_DISABLED_TOOLS",
    "DEFAULT_SYSTEM_PROMPT",
    "ENDPOINT_FAMILY_OPENCODE_HTTP",
    "ERROR_INVALID_MODEL_OUTPUT",
    "ERROR_PROVIDER_429",
    "ERROR_PROVIDER_5XX",
    "ERROR_PROVIDER_AUTH",
    "ERROR_PROVIDER_MODEL_UNAVAILABLE",
    "ERROR_REMOTE_BUDGET_EXHAUSTED",
    "ERROR_REQUEST_NOT_SUPPORTED",
    "ERROR_SEMANTIC_GATE_FAILED",
    "ERROR_SERVER_VERSION_UNSUPPORTED",
    "ERROR_STRUCTURED_OUTPUT_FAILED",
    "ERROR_TRANSPORT_NETWORK",
    "ERROR_TRANSPORT_TIMEOUT",
    "OPENCODE_PINNED_SERVER_VERSION",
    "OPENCODE_SERVER_TRANSPORT_VERSION",
    "OPENCODE_SYSTEM_PROMPT_VERSION",
    "OpenCodeError",
    "OpenCodeServerBackend",
    "OpenCodeServerBackendConfig",
    "RemoteBudget",
    "build_opencode_descriptor",
    "DEFAULT_HOSTNAME",
    "DEFAULT_USERNAME",
    "ManagedServerError",
    "ManagedServerSpec",
    "OpenCodeServerProcess",
    "BackendRuntimeConfig",
    "CompositeBackendConfig",
    "CompositeCompletionBackend",
    "LocalLlamaBackendConfig",
    "LocalRoutingBackend",
    "OpenCodeBackendConfig",
    "StrictBackendConfig",
    "build_role_adapters",
    "build_role_backend",
    "load_runtime_config",
    "EVENT_KIND_LOCAL_SWITCH",
    "EVENT_KIND_REMOTE_CALL",
    "BackendEvent",
    "CompositeRuntimeCoordinator",
    "LocalLifecycleCoordinator",
    "RemoteRuntimeCoordinator",
    "RuntimeCoordinator",
    "local_lifecycle_summary",
    "remote_calls_summary",
]
