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

__all__ = [
    "ALLOWED_REQUEST_OPTIONS",
    "ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS",
    "JSON_OBJECT_SCHEMA",
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
]
