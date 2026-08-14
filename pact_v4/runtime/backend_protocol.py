"""Transport boundary for model calls: ``CompletionBackend``.

This module is the provider boundary introduced by V4 A1 (PR 1 of the
OpenCode remote-models integration plan,
``docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md`` §5, §6,
§13, §14.1). It is a pure refactoring of the runtime layer: it separates
"how a model call is executed" (transport) from the model role
(generator / fidelity reviewer / Russian selector). The Phase 1/2
algorithms, prompts, cascade, risk and chunking stay untouched.

Design rules enforced here:

* A ``CompletionRequest`` never carries secrets; ``request_options``
  pass an allowlist and are part of backend/config identity.
* A ``CompletionResponse.text`` is always a string (mandatory
  compatibility field); ``structured`` holds the parsed object when the
  transport returned one.
* ``BackendDescriptor.identity_hash`` includes everything that can change
  the model answer (kind, model bindings, adapter version, endpoint
  family, structured-output mode + schema version, effective request
  settings, retry policy) and excludes credentials, local transport
  ports, log paths and display labels (plan §5.4).
* ``BackendCallRecord`` keeps per-call provenance (usage, latency,
  request/session ids, retry count) so callers can attribute cost and
  diagnose failures without trusting a success marker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pact_v4.phase1.models import canonical_json_hash

# Backend kinds recognised by the v4 runtime layer.
KIND_LOCAL_LLAMA = "local_llama"
KIND_OPENCODE_SERVER = "opencode_server"
KIND_COMPOSITE = "composite"

# Endpoint families that change how a request/response is interpreted
# (and therefore belong in backend identity).
ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"

# The local backend's "structured output" is the server-side
# ``response_format=json_object`` grammar. Its schema version is frozen
# so a change to the contract bumps identity instead of silently changing
# behaviour.
JSON_OBJECT_SCHEMA: Mapping[str, Any] = MappingProxyType({
    "type": "object",
    "schema_version": "pact-json-object/v1",
})

# Request options that a transport may honour, are part of backend/config
# identity (plan §5.1), and are the only keys allowed in
# ``CompletionRequest.request_options``. Unknown options are rejected so a
# typo'd or foreign option cannot silently change model behaviour.
ALLOWED_REQUEST_OPTIONS: frozenset[str] = frozenset({
    "top_p",
    "top_k",
    "seed",
    "reasoning",
})

# Keys never allowed to leak into identity / public records, even if a
# future backend erroneously places them in ``effective_options``. API key
# rotation must not invalidate cache identity (plan §11, §12).
_SECRET_KEY_TOKENS = frozenset({
    "api_key", "apikey", "password", "authorization", "bearer",
    "token", "secret", "credential", "credentials", "private_key",
})


class CompletionError(RuntimeError):
    """All non-recoverable transport failures surface as this.

    Semantic verdicts (e.g. a failed fidelity gate) are never raised here;
    they are ``GateResult`` values. This mirrors how ``ApiClientError`` is
    used today, but names the boundary rather than the transport.
    """


def _is_secret_name(name: Any) -> bool:
    """True if a key/query-param name looks like it carries credentials.

    ``token`` is matched only as a *singular whole word* (``api_token``,
    ``auth_token``): plural ``tokens`` fields such as
    ``max_output_tokens`` / ``input_tokens`` are sampling/usage settings and
    must participate in identity, never be stripped as credentials.
    """
    folded = str(name).casefold().replace("-", "_")
    if "token" in folded.split("_"):
        return True
    return any(token in folded for token in _SECRET_KEY_TOKENS if token != "token")


def _sanitize_secrets(value: Any) -> Any:
    """Recursively drop secret-bearing keys from a JSON-like value."""
    if isinstance(value, Mapping):
        return {
            key: _sanitize_secrets(item)
            for key, item in value.items()
            if not _is_secret_name(key)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_secrets(item) for item in value]
    return value


def _canonical_endpoint(endpoint: str, *, drop_port: bool) -> str:
    """Canonical form of an endpoint for identity/public records.

    Always strips URL userinfo, secret-looking query parameters and the
    fragment (plan §12: credentials never serialized). ``drop_port`` is
    used only for ``local_llama``, whose TCP port does not change the
    served model. Hostname and path are preserved so a different backend
    still produces a different identity.
    """
    if not endpoint:
        return ""
    raw = endpoint if "://" in endpoint else f"http://{endpoint}"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return endpoint
    host = parts.hostname or ""
    if not host:
        return endpoint
    if drop_port:
        netloc = host
    else:
        netloc = f"{host}:{parts.port}" if parts.port is not None else host
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_secret_name(key)
    ]
    query = "&".join(f"{key}={value}" for key, value in kept)
    return urlunsplit(
        (parts.scheme or "http", netloc, parts.path or "", query, "")
    )


@dataclass(frozen=True)
class Message:
    """One chat message sent to the backend."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("Message: role must be a non-empty string")
        if not isinstance(self.content, str):
            raise ValueError("Message: content must be a string")


@dataclass(frozen=True)
class CompletionRequest:
    """One transport-agnostic model call request.

    ``model_ref`` is a ``provider/model`` id for OpenCode backends and a
    plain model name for local OpenAI-compatible backends. ``messages`` is
    already rendered at the role-adapter layer — the transport never
    changes the prompt. ``response_schema`` describes the JSON Pact
    expects (for the local backend this selects the
    ``json_object`` grammar). ``request_options`` is an allowlist-validated
    mapping of transport options that participate in identity. No secrets
    may be placed here.
    """

    model_ref: str
    messages: tuple[Message, ...]
    max_output_tokens: int
    temperature: float
    response_schema: Mapping[str, Any] | None
    label: str
    request_options: Mapping[str, Any] = field(default_factory=dict)
    # OpenCode transport body shape: when True, the neutral system prompt
    # and the all-disabled tools map are omitted from the message body
    # (serve 1.4.7 applied a default ~32k output budget to requests that
    # carry system/tools, truncating whole-chapter generation reasoning at
    # 32000 tokens with finish=length; verified 2026-08-10). Generation-only
    # by design — audit/repair/formatting keep the historical system+tools
    # body. No-op for every other transport (local llama-server never reads
    # it). Defaults to False so the historical body shape is preserved.
    omit_system_tools: bool = False
    # Optional live-reasoning sink: called with each reasoning chunk as it
    # is produced (REASONING-STREAM). Phases pass a writer that appends to
    # the ``*_reasoning.txt`` artifact, so the file grows DURING the model
    # call (gemma_rewrite_v4 pattern) instead of only after completion.
    # When None (default) the backend delivers reasoning after completion
    # as before. Never part of identity/cache: excluded from equality and
    # repr, and transports must not send it on the wire.
    on_reasoning_chunk: Optional[Callable[[str], None]] = field(
        default=None, compare=False, repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.model_ref, str) or not self.model_ref:
            raise ValueError("CompletionRequest: model_ref must be a non-empty string")
        if not isinstance(self.messages, tuple) or not self.messages:
            raise ValueError("CompletionRequest: messages must be a non-empty tuple")
        if not all(isinstance(m, Message) for m in self.messages):
            raise ValueError("CompletionRequest: all messages must be Message objects")
        if int(self.max_output_tokens) <= 0:
            raise ValueError("CompletionRequest: max_output_tokens must be positive")
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("CompletionRequest: label must be a non-empty string")
        if not isinstance(self.omit_system_tools, bool):
            raise ValueError(
                "CompletionRequest: omit_system_tools must be a bool"
            )
        unknown = set(self.request_options) - ALLOWED_REQUEST_OPTIONS
        if unknown:
            raise ValueError(
                "CompletionRequest: unknown request option(s) "
                f"{sorted(unknown)}; allowed: {sorted(ALLOWED_REQUEST_OPTIONS)}"
            )
        if self.on_reasoning_chunk is not None and not callable(
            self.on_reasoning_chunk
        ):
            raise ValueError(
                "CompletionRequest: on_reasoning_chunk must be None or callable"
            )
        object.__setattr__(
            self,
            "request_options",
            MappingProxyType(dict(self.request_options)),
        )


@dataclass(frozen=True)
class CompletionResponse:
    """Normalized result of one model call.

    ``text`` is always present (mandatory compatibility field): if the
    transport returned a ``structured`` object instead of text, it is
    canonically re-serialized here so Pact parsers keep seeing text. Pact
    re-validates JSON and PID/schema invariants regardless.
    """

    text: str = ""
    structured: Mapping[str, Any] | None = None
    provider: str = ""
    model: str = ""
    finish_reason: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    wall_seconds: float = 0.0
    request_id: str | None = None
    session_id: str | None = None
    retry_count: int = 0
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text and self.structured is not None:
            object.__setattr__(
                self,
                "text",
                json.dumps(
                    self.structured,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        usage = self.usage if isinstance(self.usage, Mapping) else {}
        object.__setattr__(self, "usage", MappingProxyType(dict(usage)))
        metadata = self.raw_metadata if isinstance(self.raw_metadata, Mapping) else {}
        object.__setattr__(self, "raw_metadata", MappingProxyType(dict(metadata)))


@dataclass(frozen=True)
class BackendCallRecord:
    """Per-call provenance kept by a ``CompletionBackend``.

    ``request_id``/``session_id`` are transport-assigned ids (OpenCode
    session/request ids, provider request ids) used to attribute real
    paid calls across retries; the local backend reports ``None`` for
    both because llama-server does not expose them.
    """

    label: str
    model_ref: str
    request_id: str | None
    session_id: str | None
    retry_count: int
    finish_reason: str | None
    usage: Mapping[str, Any]
    wall_seconds: float
    raw_metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "usage", MappingProxyType(dict(self.usage if self.usage else {}))
        )
        object.__setattr__(
            self,
            "raw_metadata",
            MappingProxyType(dict(self.raw_metadata if self.raw_metadata else {})),
        )


@dataclass(frozen=True)
class BackendDescriptor:
    """Identity of one backend, independent of any single call.

    ``identity_hash`` is a deterministic sha256 over every field that can
    change the model answer; it is recomputed from content in
    ``__post_init__`` and never caller-supplied. Credentials (and fields
    that do not affect model behaviour, such as the local TCP port of
    ``local_llama``) are excluded — API key rotation therefore does not
    change cache/resume identity. Hostname and path of the endpoint are
    preserved (canonicalized), so a different backend still gets a
    different identity.
    """

    kind: str
    transport_version: str
    endpoint_family: str
    public_endpoint: str
    model_bindings: Mapping[str, str]
    effective_options: Mapping[str, Any]
    identity_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("BackendDescriptor: kind must be a non-empty string")
        if not isinstance(self.transport_version, str) or not self.transport_version:
            raise ValueError(
                "BackendDescriptor: transport_version must be a non-empty string"
            )
        if not isinstance(self.endpoint_family, str) or not self.endpoint_family:
            raise ValueError(
                "BackendDescriptor: endpoint_family must be a non-empty string"
            )
        object.__setattr__(
            self,
            "model_bindings",
            MappingProxyType(dict(self.model_bindings)),
        )
        object.__setattr__(
            self,
            "effective_options",
            MappingProxyType(dict(self.effective_options)),
        )
        object.__setattr__(
            self,
            "identity_hash",
            canonical_json_hash(self._identity_payload()),
        )

    def _identity_payload(self) -> dict:
        # Secrets are stripped defensively before hashing: even if a future
        # backend accidentally places an api key / password in
        # effective_options, credential rotation must not change identity
        # (plan §5.4, §11). The endpoint is canonicalized; the local TCP
        # port is dropped only for ``local_llama``.
        return {
            "artifact": "pact-v4-backend-descriptor/v1",
            "kind": self.kind,
            "transport_version": self.transport_version,
            "endpoint_family": self.endpoint_family,
            "endpoint": _canonical_endpoint(
                self.public_endpoint, drop_port=(self.kind == KIND_LOCAL_LLAMA)
            ),
            "model_bindings": dict(sorted(self.model_bindings.items())),
            "effective_options": _sanitize_secrets(dict(self.effective_options)),
        }

    def public_record(self) -> Mapping[str, Any]:
        """Sanitized record safe for artifacts/logs (no credentials)."""
        return {
            "kind": self.kind,
            "transport_version": self.transport_version,
            "endpoint_family": self.endpoint_family,
            "public_endpoint": _canonical_endpoint(self.public_endpoint, drop_port=False),
            "model_bindings": dict(sorted(self.model_bindings.items())),
            "effective_options": _sanitize_secrets(dict(self.effective_options)),
            "identity_hash": self.identity_hash,
        }


class CompletionBackend(Protocol):
    """Transport boundary: one object that executes model calls."""

    @property
    def descriptor(self) -> BackendDescriptor:
        ...

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        ...

    def close(self) -> None:
        ...

    def call_records(self) -> Sequence[BackendCallRecord]:
        ...


__all__ = [
    "KIND_LOCAL_LLAMA",
    "KIND_OPENCODE_SERVER",
    "KIND_COMPOSITE",
    "ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS",
    "JSON_OBJECT_SCHEMA",
    "ALLOWED_REQUEST_OPTIONS",
    "CompletionError",
    "Message",
    "CompletionRequest",
    "CompletionResponse",
    "BackendCallRecord",
    "BackendDescriptor",
    "CompletionBackend",
]
