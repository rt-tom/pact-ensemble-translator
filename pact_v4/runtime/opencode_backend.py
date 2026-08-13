"""``OpenCodeServerBackend`` — remote model transport over ``opencode serve``.

A ``CompletionBackend`` implementation (V4 integration plan, PR 2 / Поток C,
``docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md`` §7, §10,
§13, §14.2) that talks to a *pre-started* ``opencode serve`` HTTP/OpenAPI
server directly. No TypeScript sidecar / Node SDK: the Python client speaks
the REST contract of the installed server.

Version pin (mandatory, plan §7.1)
----------------------------------

This adapter is contract-pinned to **opencode 1.4.7**. The request/response
fields were verified against three sources, not just the web docs:

* the generated SDK types at tag ``v1.4.7``
  (``packages/sdk/js/src/gen/types.gen.ts``);
* the server source at tag ``v1.4.7``
  (``packages/opencode/src/session/prompt.ts``, ``message-v2.ts``,
  ``llm.ts``);
* a live ``opencode serve`` probe (health/provider/agent/tool-ids).

Pinned facts used here:

* ``GET /global/health`` -> ``{"healthy": true, "version": "1.4.7"}``;
* ``GET /provider`` -> ``{all: [Provider], default: {...}, connected: [...]}``;
* ``POST /session`` ``{title?}`` -> ``Session{id, ...}``;
* ``DELETE /session/{id}`` -> ``boolean``;
* ``POST /session/{id}/message`` ``{model?, agent?, system?, tools?,
  format?, parts}`` -> ``{info: AssistantMessage, parts: Part[]}``;
* ``GET /experimental/tool/ids`` -> ``string[]`` (used to build the
  all-tools-disabled map).

``json_schema`` structured output is sent via the message-body ``format``
field. v1.4.7 accepts ``retryCount`` in ``format`` but performs a single
attempt (``StructuredOutputError`` with ``retries: 0`` on failure), so this
backend implements the bounded structured-output retry itself (§7.4).

Design rules (plan §5, §7, §10, §12)
-------------------------------------

* Read-only preflight before the first model call: health + version policy,
  provider connected, model exists, tool IDs for the disabled-tools map.
* One isolated session per work unit: ``session_scope=per_request``,
  ``context_reuse=false``, every message carries an explicit
  ``provider/model`` and ``tools`` (all disabled); ``close()`` deletes only
  sessions this backend created, and only when the session policy allows.
* ``BackendDescriptor`` includes everything that can change the model answer
  (model bindings, adapter/server contract version, endpoint family,
  agent/system identity, structured-output mode + schema version, effective
  options, retry policy) and excludes credentials and local paths.
* Per-request ``temperature``/``max_output_tokens`` are recorded in identity
  but *not* sent in the v1.4.7 message body (the server has no per-request
  sampling fields; agent/model defaults apply). Non-empty
  ``request_options`` are rejected loudly rather than silently ignored —
  except the V4.1 ``reasoning`` option, which maps to the server's
  top-level ``reasoningEffort`` field (1=low, 2=medium, 3=high).
* Error normalization per §10; no silent model fallback; semantic
  ``passed=False`` is never retried as a transport error (the backend never
  interprets verdicts — it returns text/structured and the Pact layer gates).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, NoReturn, Optional, Sequence, Tuple
from urllib.parse import quote

import requests

from pact_v4.runtime.backend_protocol import (
    KIND_OPENCODE_SERVER,
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
)

LOG = logging.getLogger(__name__)

# Adapter / transport version of this opencode-server HTTP adapter. Bump
# when the pinned request/response contract changes. The "v1.4" suffix
# mirrors the server minor the adapter is contract-pinned to.
OPENCODE_SERVER_TRANSPORT_VERSION = "opencode-server-http/v1.4"

# Endpoint family discriminates request/response interpretation (and thus
# backend identity). The value matches the family string already used in
# tests/pact_v4/runtime/test_backend_protocol.py.
ENDPOINT_FAMILY_OPENCODE_HTTP = "opencode_http"

# Server version this adapter was developed and verified against.
OPENCODE_PINNED_SERVER_VERSION = "1.4.7"

# Short neutral system prompt (plan §7.3) sent with every message so the
# model receives no agentic/coding instructions beyond the Pact prompt.
OPENCODE_SYSTEM_PROMPT_VERSION = "pact-v4-neutral/v1"
DEFAULT_SYSTEM_PROMPT = (
    "Follow the user's instructions exactly. Do not use tools, do not read "
    "files, do not execute commands. Return only the requested output."
)

# Session title prefix so sessions created by this backend are identifiable
# on the server (diagnostics only; never parsed by Pact).
SESSION_TITLE_PREFIX = "pact-v4:"

# Tool IDs disabled for every message when ``tools_disabled=True``. The full
# list is refreshed from ``/experimental/tool/ids`` at preflight; this is the
# fallback set used when that endpoint is unavailable.
DEFAULT_DISABLED_TOOLS: Tuple[str, ...] = (
    "bash",
    "read",
    "edit",
    "write",
    "glob",
    "grep",
    "webfetch",
    "task",
    "todowrite",
    "websearch",
    "codesearch",
    "skill",
    "apply_patch",
    "question",
)

# Error classes (plan §10). The transport raises ``OpenCodeError`` with one
# of these on ``error_class``. ``semantic_gate_failed`` is never raised by the
# transport — it exists so callers can distinguish a transport failure from a
# Pact semantic verdict (which is a ``GateResult``, never a transport error).
ERROR_TRANSPORT_TIMEOUT = "transport_timeout"
ERROR_TRANSPORT_NETWORK = "transport_network"
ERROR_PROVIDER_429 = "provider_429"
ERROR_PROVIDER_5XX = "provider_5xx"
ERROR_PROVIDER_AUTH = "provider_auth"
ERROR_PROVIDER_MODEL_UNAVAILABLE = "provider_model_unavailable"
ERROR_STRUCTURED_OUTPUT_FAILED = "structured_output_failed"
ERROR_INVALID_MODEL_OUTPUT = "invalid_model_output"
ERROR_SEMANTIC_GATE_FAILED = "semantic_gate_failed"
ERROR_SERVER_VERSION_UNSUPPORTED = "server_version_unsupported"
ERROR_REMOTE_BUDGET_EXHAUSTED = "remote_budget_exhausted"
# Request-level incompatibility (e.g. unsupported request_options, or
# json_schema mode without a response_schema). Raised before any network
# call; the request never reached the provider.
ERROR_REQUEST_NOT_SUPPORTED = "request_not_supported"

# error classes that are retried (bounded) by this transport.
_RETRYABLE_ERROR_CLASSES = frozenset({
    ERROR_TRANSPORT_TIMEOUT,
    ERROR_TRANSPORT_NETWORK,
    ERROR_PROVIDER_5XX,
    ERROR_PROVIDER_429,
})

# Structured-output failures are retried separately, bounded by the
# configured structured-output retry budget (plan §7.4 / §10).
_STRUCTURED_RETRYABLE_ERROR_CLASSES = frozenset({ERROR_STRUCTURED_OUTPUT_FAILED})

# Status codes that are never retried (plan §10: 401/403 no retry).
_AUTH_STATUS_CODES = frozenset({401, 403})


class OpenCodeError(CompletionError):
    """Transport error carrying the normalized §10 error class.

    ``error_class`` lets callers map the failure to the right lifecycle
    status (``invalid_model_output``/``incomplete_generation`` for schema
    failure, operational debt for budget exhaustion, etc.) instead of a
    semantic gate verdict.
    """

    def __init__(
        self,
        error_class: str,
        message: str,
        *,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.status_code = status_code
        self.retry_after = retry_after
        self.session_id = session_id
        self.request_id = request_id
        self.attempts = attempts


@dataclass(frozen=True)
class RemoteBudget:
    """Run budgets for remote calls (plan §10).

    Budget exhaustion is an explicit operational failure, never a semantic
    verdict.

    ``max_requests_per_chapter`` default 500 (B11): run_003_remote showed a
    full chapter cycle needs ~350-400 requests (16 chunks x (2 generation +
    2 fidelity + ~0.5 selection) + audit 2x16 + convergence re-audit +
    repair + formatting + quarantined retry + retry reserve); the earlier
    250-per-chapter default was exhausted at the end of the cycle
    (convergence/repair/formatting went to debt, narrator_gender defect was
    not fixed). 500 keeps a reserve. The budget is part of the backend
    identity (``build_opencode_descriptor``), so a run that changes this
    value must use a fresh ``--out-dir``/``--run-label``.
    """

    max_requests_per_chapter: int = 500
    max_retry_requests_per_chapter: int = 10
    max_wait_seconds_on_rate_limit: float = 900.0
    max_reported_cost: Optional[float] = None

    def __post_init__(self) -> None:
        if self.max_requests_per_chapter <= 0:
            raise ValueError("RemoteBudget: max_requests_per_chapter must be positive")
        if self.max_retry_requests_per_chapter < 0:
            raise ValueError("RemoteBudget: max_retry_requests_per_chapter must be >= 0")
        if self.max_wait_seconds_on_rate_limit < 0:
            raise ValueError("RemoteBudget: max_wait_seconds_on_rate_limit must be >= 0")


@dataclass(frozen=True)
class OpenCodeServerBackendConfig:
    """Identity-relevant settings of an ``OpenCodeServerBackend``.

    ``username``/``password`` may be provided directly or resolved from the
    named environment variables. They never enter the descriptor identity or
    ``public_record()``. ``base_url`` is the public endpoint of the
    pre-started ``opencode serve`` (plan §7.1).
    """

    base_url: str = "http://127.0.0.1:4096"
    server_version_policy: str = "compatible_minor"  # exact | compatible_minor
    pinned_server_version: str = OPENCODE_PINNED_SERVER_VERSION
    username_env: str = "OPENCODE_SERVER_USERNAME"
    password_env: str = "OPENCODE_SERVER_PASSWORD"
    username: Optional[str] = None
    password: Optional[str] = None

    # Agent isolation (plan §7.3): a dedicated tool-less agent name, or None
    # to use the server default agent (tools are still disabled via the
    # message-body tools map).
    agent: Optional[str] = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    system_prompt_version: str = OPENCODE_SYSTEM_PROMPT_VERSION

    # Session policy (plan §7.2).
    session_scope: str = "per_request"
    retain_success_sessions: bool = False
    retain_failed_sessions: bool = True

    # Tools / structured output.
    tools_disabled: bool = True
    structured_output_mode: str = "prompt_only"  # prompt_only | json_schema
    structured_output_retry_count: int = 2

    # Transport retry policy (plan §10).
    # TIMEOUT-FIX (2026-08-13): whole-chapter generation with
    # --reasoning 3 (high) routinely exceeds 10 minutes (run_remote_003:
    # attempt 2 was cut at exactly 600s; 001/002 took 7-9:45 min; 003
    # streamed 155k delta events for 10+ min). Default raised 600 -> 2400
    # (40 min) so a long generation is NOT aborted by the transport; the
    # value is part of backend identity (build_opencode_descriptor), so a
    # config that changes it needs a fresh --out-dir/--run-label.
    timeout_seconds: float = 2400.0
    http_retries: int = 2
    retry_delay_seconds: float = 5.0

    # Remote budgets (plan §10).
    remote_budget: RemoteBudget = field(default_factory=RemoteBudget)

    # Role -> provider/model bindings (part of backend identity).
    model_bindings: Mapping[str, str] = field(default_factory=dict)

    # Effective sampling settings (plan §5.4). v1.4.7 cannot send these in
    # the message body, but they belong in backend identity so a change in
    # requested sampling invalidates cache/resume instead of silently reusing
    # a candidate generated with different settings.
    default_temperature: Optional[float] = None
    default_max_output_tokens: Optional[int] = None

    transport_version: str = OPENCODE_SERVER_TRANSPORT_VERSION
    endpoint_family: str = ENDPOINT_FAMILY_OPENCODE_HTTP

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_bindings", dict(self.model_bindings))
        if not self.base_url:
            raise ValueError("OpenCodeServerBackendConfig: base_url must not be empty")
        if self.server_version_policy not in ("exact", "compatible_minor"):
            raise ValueError(
                "OpenCodeServerBackendConfig: unknown server_version_policy "
                f"{self.server_version_policy!r}"
            )
        if self.session_scope != "per_request":
            raise ValueError(
                f"OpenCodeServerBackendConfig: only session_scope='per_request' "
                f"is supported; got {self.session_scope!r}"
            )
        if self.structured_output_mode not in ("prompt_only", "json_schema"):
            raise ValueError(
                "OpenCodeServerBackendConfig: unknown structured_output_mode "
                f"{self.structured_output_mode!r}"
            )
        if self.structured_output_retry_count < 0:
            raise ValueError(
                "OpenCodeServerBackendConfig: structured_output_retry_count must be >= 0"
            )
        if self.http_retries < 0:
            raise ValueError("OpenCodeServerBackendConfig: http_retries must be >= 0")
        if self.retry_delay_seconds < 0:
            raise ValueError("OpenCodeServerBackendConfig: retry_delay_seconds must be >= 0")
        if self.timeout_seconds <= 0:
            raise ValueError("OpenCodeServerBackendConfig: timeout_seconds must be positive")
        if self.default_temperature is not None and (
            not isinstance(self.default_temperature, (int, float))
            or self.default_temperature < 0
        ):
            raise ValueError(
                "OpenCodeServerBackendConfig: default_temperature must be >= 0"
            )
        if self.default_max_output_tokens is not None and (
            int(self.default_max_output_tokens) <= 0
        ):
            raise ValueError(
                "OpenCodeServerBackendConfig: default_max_output_tokens must be positive"
            )


def _parse_model_ref(model_ref: str) -> Tuple[str, str]:
    """Split a ``provider/model`` reference into ``(provider, model)``."""
    parts = model_ref.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CompletionError(
            f"OpenCodeServerBackend: model_ref {model_ref!r} is not "
            "'provider/model'"
        )
    return parts[0], parts[1]


def build_opencode_descriptor(
    cfg: "OpenCodeServerBackendConfig",
) -> BackendDescriptor:
    """Build the backend identity descriptor for an OpenCode config.

    Module-level (not a backend method) so config loaders can compute
    ``identity_hash`` without constructing a backend / HTTP session.
    Includes everything that can change the model answer (bindings,
    adapter/server contract version, endpoint family, agent/system
    identity, structured-output mode + schema version, effective options,
    retry policy) and excludes credentials (plan §5.4, §12).
    """
    bindings = dict(cfg.model_bindings) or {"default": ""}
    effective_options = {
        "server_version_policy": cfg.server_version_policy,
        "pinned_server_version": cfg.pinned_server_version,
        "agent": cfg.agent or "server-default",
        "system_prompt_version": cfg.system_prompt_version,
        "session_policy": {
            "scope": cfg.session_scope,
            "retain_success": cfg.retain_success_sessions,
            "retain_failed": cfg.retain_failed_sessions,
        },
        "tools_disabled": cfg.tools_disabled,
        "structured_output": {
            "mode": cfg.structured_output_mode,
            "schema_version": "pact-json-object/v1",
            "retry_count": cfg.structured_output_retry_count,
        },
        "temperature": cfg.default_temperature,
        "max_output_tokens": cfg.default_max_output_tokens,
        "timeout_seconds": cfg.timeout_seconds,
        "http_retries": cfg.http_retries,
        "retry_delay_seconds": cfg.retry_delay_seconds,
        "remote_budget": {
            "max_requests_per_chapter": cfg.remote_budget.max_requests_per_chapter,
            "max_retry_requests_per_chapter": cfg.remote_budget.max_retry_requests_per_chapter,
            "max_wait_seconds_on_rate_limit": cfg.remote_budget.max_wait_seconds_on_rate_limit,
        },
    }
    return BackendDescriptor(
        kind=KIND_OPENCODE_SERVER,
        transport_version=cfg.transport_version,
        endpoint_family=cfg.endpoint_family,
        public_endpoint=cfg.base_url,
        model_bindings=bindings,
        effective_options=effective_options,
    )


def _major_minor(version: str) -> Tuple[int, int]:
    pieces = version.split(".")
    try:
        return int(pieces[0]), int(pieces[1])
    except (IndexError, ValueError):
        raise ValueError(f"cannot parse version {version!r}")


def _version_compatible(
    server_version: str, *, policy: str, pinned: str
) -> bool:
    """Version-policy check against the pinned adapter version."""
    if policy == "exact":
        return server_version == pinned
    return _major_minor(server_version) == _major_minor(pinned)


def _normalize_usage(info: Mapping[str, Any]) -> dict:
    """Normalize AssistantMessage ``tokens``/``cost`` into usage.

    Only values actually reported by the provider are included; missing
    fields are omitted rather than invented (plan §9.3).
    """
    tokens = info.get("tokens") if isinstance(info.get("tokens"), Mapping) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
    usage: dict = {}
    if "input" in tokens and tokens.get("input") is not None:
        usage["input_tokens"] = tokens.get("input")
    if "output" in tokens and tokens.get("output") is not None:
        usage["output_tokens"] = tokens.get("output")
    if "reasoning" in tokens and tokens.get("reasoning") is not None:
        usage["reasoning_tokens"] = tokens.get("reasoning")
    if "read" in cache and cache.get("read") is not None:
        usage["cached_input_tokens"] = cache.get("read")
    if "write" in cache and cache.get("write") is not None:
        usage["cached_write_tokens"] = cache.get("write")
    cost = info.get("cost")
    if cost is not None:
        usage["reported_cost"] = cost
    return usage


class OpenCodeServerBackend:
    """``CompletionBackend`` over a pre-started ``opencode serve`` server.

    Accepts an injected ``requests.Session`` (or a session-like fake) so the
    fake-server contract suite runs fully offline.
    """

    def __init__(
        self,
        config: Optional[OpenCodeServerBackendConfig] = None,
        *,
        session: Optional[Any] = None,
    ) -> None:
        self._cfg = config or OpenCodeServerBackendConfig()
        self._session = session or requests.Session()
        username = self._cfg.username
        password = self._cfg.password
        if username is None:
            username = os.environ.get(self._cfg.username_env) or None
        if password is None:
            password = os.environ.get(self._cfg.password_env) or None
        self._username = username
        self._password = password

        self._closed = False
        self._preflight_done = False
        self._server_version: Optional[str] = None
        self._connected_providers: frozenset = frozenset()
        self._provider_models: dict = {}
        self._tool_ids: Tuple[str, ...] = DEFAULT_DISABLED_TOOLS
        self._descriptor: Optional[BackendDescriptor] = None

        self._records: list[BackendCallRecord] = []
        # D1: optional per-call usage sink (a UsageRecordWriter.write_call).
        # Invoked at the exact moment a call completes (success or final
        # failure) so usage.ndjson is written per completed call — a crash
        # inside a phase never loses already-completed calls. Each call is
        # appended to ``_records`` exactly once, so a resumed run (fresh
        # backend) can never duplicate an already-journaled call.
        self._usage_sink: Optional[Any] = None
        # Sessions created by THIS backend: {session_id: outcome}
        # outcome in {"success", "failed"}. Used for close() cleanup and to
        # prove we never touch foreign sessions.
        self._owned_sessions: dict[str, str] = {}

        # Budget counters (plan §10).
        self._request_count = 0
        self._retry_request_count = 0
        self._wait_seconds = 0.0

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        base = self._cfg.base_url.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    def _auth(self) -> Optional[Tuple[str, str]]:
        if not self._password:
            return None
        return (self._username or "opencode", self._password)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> requests.Response:
        kwargs: dict = {
            "timeout": self._cfg.timeout_seconds,
        }
        auth = self._auth()
        if auth is not None:
            kwargs["auth"] = auth
        if json_body is not None:
            kwargs["json"] = dict(json_body)
        try:
            return self._session.request(method, self._url(path), **kwargs)
        except requests.exceptions.Timeout as exc:
            raise OpenCodeError(
                ERROR_TRANSPORT_TIMEOUT,
                f"opencode server request to {path} timed out: {exc}",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server connection error on {path}: {exc}",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server request to {path} failed: {exc}",
            ) from exc

    def _raise_http_error(
        self, resp: requests.Response, *, path: str
    ) -> None:
        """Map a non-2xx HTTP response to an ``OpenCodeError`` (§10)."""
        status = resp.status_code
        retry_after = _parse_retry_after(resp)
        if status in _AUTH_STATUS_CODES:
            raise OpenCodeError(
                ERROR_PROVIDER_AUTH,
                f"opencode server {path} auth failed (HTTP {status})",
                status_code=status,
            )
        if status == 429:
            raise OpenCodeError(
                ERROR_PROVIDER_429,
                f"opencode server {path} rate limited (HTTP 429)",
                status_code=status,
                retry_after=retry_after,
            )
        if 500 <= status < 600:
            raise OpenCodeError(
                ERROR_PROVIDER_5XX,
                f"opencode server {path} failed (HTTP {status})",
                status_code=status,
            )
        if status == 404:
            raise OpenCodeError(
                ERROR_PROVIDER_MODEL_UNAVAILABLE,
                f"opencode server {path} not found (HTTP 404)",
                status_code=status,
            )
        # Any other non-2xx is a provider-side failure, not retryable.
        raise OpenCodeError(
            ERROR_TRANSPORT_NETWORK,
            f"opencode server {path} returned HTTP {status}",
            status_code=status,
        )

    def _get_json(self, path: str) -> Any:
        resp = self._request_json("GET", path)
        if not 200 <= resp.status_code < 300:
            self._raise_http_error(resp, path=path)
        try:
            return resp.json()
        except ValueError as exc:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server {path} returned malformed JSON: {exc}",
                status_code=resp.status_code,
            ) from exc

    # ------------------------------------------------------------------
    # Preflight (read-only, plan §7.1)
    # ------------------------------------------------------------------

    def preflight(self) -> Mapping[str, Any]:
        """Run the read-only preflight checks once and cache the result.

        Raises ``OpenCodeError`` (before any model call) when the server is
        unavailable, the version is unsupported, the provider is not
        connected, or the model is missing. No silent fallback.
        """
        if self._preflight_done:
            return self._preflight_report()

        # 1. Health + version.
        health = self._get_json("/global/health")
        healthy = bool(health.get("healthy"))
        version = str(health.get("version") or "")
        if not healthy:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server reports unhealthy: {health!r}",
            )
        if not version:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                "opencode server health did not report a version",
            )
        self._server_version = version
        if not _version_compatible(
            version,
            policy=self._cfg.server_version_policy,
            pinned=self._cfg.pinned_server_version,
        ):
            raise OpenCodeError(
                ERROR_SERVER_VERSION_UNSUPPORTED,
                f"opencode server version {version!r} is not compatible with "
                f"adapter policy {self._cfg.server_version_policy!r} pinned to "
                f"{self._cfg.pinned_server_version!r}",
            )

        # 2. Provider connection + model existence (authoritative /provider).
        provider = self._get_json("/provider")
        all_providers = provider.get("all") if isinstance(provider.get("all"), list) else []
        self._connected_providers = frozenset(
            provider.get("connected") or []
        )
        self._provider_models = {
            p.get("id"): set((p.get("models") or {}).keys())
            for p in all_providers
            if isinstance(p, dict) and p.get("id")
        }

        # Verify every configured binding now (fail loudly, before any call).
        for role, model_ref in self._cfg.model_bindings.items():
            provider_id, model_id = _parse_model_ref(model_ref)
            self._check_provider_model(provider_id, model_id, role=role)

        # 3. Tool IDs for the all-disabled tools map (best-effort).
        try:
            tool_ids = self._get_json("/experimental/tool/ids")
            if isinstance(tool_ids, list):
                ids = tuple(str(t) for t in tool_ids if str(t))
                if ids:
                    self._tool_ids = tuple(dict.fromkeys(ids))
        except OpenCodeError as exc:
            LOG.warning(
                "OpenCodeServerBackend: could not refresh tool ids, using "
                "fallback set: %s",
                exc,
            )

        self._preflight_done = True
        return self._preflight_report()

    def _preflight_report(self) -> Mapping[str, Any]:
        return {
            "server_version": self._server_version,
            "server_version_policy": self._cfg.server_version_policy,
            "pinned_server_version": self._cfg.pinned_server_version,
            "connected_providers": sorted(self._connected_providers),
            "model_bindings": dict(self._cfg.model_bindings),
            "tools_disabled": self._cfg.tools_disabled,
            "disabled_tool_count": len(self._tool_ids),
        }

    def _check_provider_model(
        self, provider_id: str, model_id: str, *, role: Optional[str] = None
    ) -> None:
        where = f"for role {role!r}" if role else "for the request"
        if provider_id not in self._connected_providers:
            raise OpenCodeError(
                ERROR_PROVIDER_MODEL_UNAVAILABLE,
                f"opencode provider {provider_id!r} is not connected {where}",
            )
        models = self._provider_models.get(provider_id, set())
        if model_id not in models:
            raise OpenCodeError(
                ERROR_PROVIDER_MODEL_UNAVAILABLE,
                f"opencode model {provider_id}/{model_id} does not exist {where}",
            )

    # ------------------------------------------------------------------
    # Session / message lifecycle (plan §7.2)
    # ------------------------------------------------------------------

    def _create_session(self, label: str) -> str:
        self._check_budget_request()
        body = {"title": f"{SESSION_TITLE_PREFIX}{label}"}
        resp = self._request_json("POST", "/session", json_body=body)
        if not 200 <= resp.status_code < 300:
            self._raise_http_error(resp, path="/session")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server /session returned malformed JSON: {exc}",
                status_code=resp.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server /session response is not an object: {data!r}",
                status_code=resp.status_code,
            )
        session_id = data.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server /session response missing session id: {data!r}",
            )
        self._owned_sessions[session_id] = "failed"
        return session_id

    def _delete_own_session(self, session_id: str) -> None:
        """Delete a session this backend created (best-effort)."""
        if session_id not in self._owned_sessions:
            # Never touch a foreign session.
            return
        try:
            resp = self._request_json("DELETE", f"/session/{quote(session_id)}")
            if not 200 <= resp.status_code < 300:
                LOG.warning(
                    "OpenCodeServerBackend: failed to delete own session "
                    "%s (HTTP %s)",
                    session_id,
                    resp.status_code,
                )
            else:
                self._owned_sessions.pop(session_id, None)
        except OpenCodeError as exc:
            LOG.warning(
                "OpenCodeServerBackend: failed to delete own session %s: %s",
                session_id,
                exc,
            )

    def _build_message_body(
        self,
        request: CompletionRequest,
        *,
        provider_id: str,
        model_id: str,
    ) -> dict:
        body: dict = {
            "model": {"providerID": provider_id, "modelID": model_id},
            "system": self._cfg.system_prompt,
            "parts": [
                {"type": "text", "text": msg.content}
                for msg in request.messages
            ],
        }
        if self._cfg.agent:
            body["agent"] = self._cfg.agent
        if self._cfg.tools_disabled:
            body["tools"] = {tool_id: False for tool_id in self._tool_ids}
        if self._cfg.structured_output_mode == "json_schema" and request.response_schema is not None:
            body["format"] = {
                "type": "json_schema",
                "schema": dict(request.response_schema),
                "retryCount": self._cfg.structured_output_retry_count,
            }
        reasoning = request.request_options.get("reasoning")
        if reasoning:
            # V4.1: opencode serve 1.4.7 honours a top-level
            # ``reasoningEffort`` on POST /session/{id}/message (empirically
            # verified 2026-08-08: high -> 23 reasoning tokens, absent -> 0).
            # The GenerationParams contract restricts reasoning to {0,1,2,3},
            # so an out-of-range value here is a programming error — fail
            # loudly instead of silently dropping the budget.
            effort = {1: "low", 2: "medium", 3: "high"}.get(reasoning)
            if effort is None:
                raise OpenCodeError(
                    ERROR_REQUEST_NOT_SUPPORTED,
                    "OpenCodeServerBackend: unsupported reasoning level "
                    f"{reasoning!r} in request_options (allowed: 1=low, "
                    "2=medium, 3=high)",
                )
            body["reasoningEffort"] = effort
        return body

    def _post_message(
        self,
        session_id: str,
        request: CompletionRequest,
        *,
        provider_id: str,
        model_id: str,
    ) -> Tuple[Mapping[str, Any], Mapping[str, Any], int]:
        """POST one message; return ``(info, parts, status)``.

        Raises ``OpenCodeError`` for HTTP-level failures; message-level
        failures are returned as ``info`` with ``error`` set and are mapped
        by the caller.
        """
        body = self._build_message_body(request, provider_id=provider_id, model_id=model_id)
        path = f"/session/{quote(session_id)}/message"
        resp = self._request_json("POST", path, json_body=body)
        if not 200 <= resp.status_code < 300:
            self._raise_http_error(resp, path=path)
        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server {path} returned malformed JSON: {exc}",
                status_code=resp.status_code,
            ) from exc
        info = data.get("info") if isinstance(data, dict) else None
        parts = data.get("parts") if isinstance(data, dict) else None
        if not isinstance(info, Mapping) or not isinstance(parts, list):
            raise OpenCodeError(
                ERROR_TRANSPORT_NETWORK,
                f"opencode server {path} response missing info/parts: {data!r}",
                status_code=resp.status_code,
            )
        return info, parts, resp.status_code

    def _extract_text(self, parts: Sequence[Mapping[str, Any]]) -> str:
        """Concatenate non-synthetic assistant ``text`` parts (v1.4.7)."""
        chunks = []
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") != "text":
                continue
            if part.get("synthetic") or part.get("ignored"):
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
        return "".join(chunks)

    def _map_message_error(
        self, info: Mapping[str, Any], *, session_id: str
    ) -> Optional[OpenCodeError]:
        """Map a message-level ``info.error`` to an ``OpenCodeError``."""
        error = info.get("error")
        if not isinstance(error, Mapping):
            return None
        name = error.get("name")
        data = error.get("data") if isinstance(error.get("data"), Mapping) else {}
        message = str(data.get("message") or error.get("message") or name or "unknown error")
        request_id = info.get("id")
        if name == "ProviderAuthError":
            return OpenCodeError(
                ERROR_PROVIDER_AUTH, message, session_id=session_id, request_id=request_id
            )
        if name == "StructuredOutputError":
            return OpenCodeError(
                ERROR_STRUCTURED_OUTPUT_FAILED,
                message,
                session_id=session_id,
                request_id=request_id,
            )
        if name in ("MessageOutputLengthError", "ContextOverflowError"):
            return OpenCodeError(
                ERROR_INVALID_MODEL_OUTPUT, message, session_id=session_id, request_id=request_id
            )
        if name == "APIError":
            status = data.get("statusCode")
            try:
                status_int = int(status) if status is not None else None
            except (TypeError, ValueError):
                status_int = None
            if status_int in _AUTH_STATUS_CODES:
                cls = ERROR_PROVIDER_AUTH
            elif status_int == 429:
                cls = ERROR_PROVIDER_429
            elif status_int is not None and 500 <= status_int < 600:
                cls = ERROR_PROVIDER_5XX
            else:
                cls = ERROR_TRANSPORT_NETWORK
            return OpenCodeError(
                cls, message, status_code=status, session_id=session_id, request_id=request_id
            )
        # UnknownError / anything else: not retryable, no fallback.
        return OpenCodeError(
            ERROR_TRANSPORT_NETWORK,
            message,
            session_id=session_id,
            request_id=request_id,
        )

    # ------------------------------------------------------------------
    # Budgets (plan §10)
    # ------------------------------------------------------------------

    def _check_budget_request(self) -> None:
        if self._request_count >= self._cfg.remote_budget.max_requests_per_chapter:
            raise OpenCodeError(
                ERROR_REMOTE_BUDGET_EXHAUSTED,
                "remote budget exhausted: "
                f"max_requests_per_chapter={self._cfg.remote_budget.max_requests_per_chapter}",
            )
        self._request_count += 1

    def _can_retry(self) -> bool:
        """Whether one more retry request is within the retry budget."""
        return (
            self._retry_request_count
            < self._cfg.remote_budget.max_retry_requests_per_chapter
        )

    def _reserve_retry(self) -> None:
        self._retry_request_count += 1

    def _consume_wait_budget(self, seconds: float) -> None:
        budget = self._cfg.remote_budget.max_wait_seconds_on_rate_limit
        if self._wait_seconds + seconds > budget:
            raise OpenCodeError(
                ERROR_REMOTE_BUDGET_EXHAUSTED,
                "remote budget exhausted: "
                f"max_wait_seconds_on_rate_limit={budget}",
            )
        self._wait_seconds += seconds

    # ------------------------------------------------------------------
    # CompletionBackend protocol
    # ------------------------------------------------------------------

    @property
    def descriptor(self) -> BackendDescriptor:
        if self._descriptor is None:
            self._descriptor = self._build_descriptor()
        return self._descriptor

    def _build_descriptor(self) -> BackendDescriptor:
        return build_opencode_descriptor(self._cfg)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._closed:
            raise CompletionError(
                "OpenCodeServerBackend: backend is closed; cannot complete a request"
            )
        if request.request_options:
            # v1.4.7 has no per-request sampling fields except the top-level
            # ``reasoningEffort`` (V4.1, transported via request_options
            # key "reasoning"); silently dropping any other option would
            # change behaviour without being honest (plan §5.1).
            unsupported = set(request.request_options) - {"reasoning"}
            if unsupported:
                raise OpenCodeError(
                    ERROR_REQUEST_NOT_SUPPORTED,
                    "OpenCodeServerBackend: request_options are not supported by "
                    f"opencode-server-http/v1.4 (got {sorted(request.request_options)})",
                )
        if self._cfg.structured_output_mode == "json_schema" and request.response_schema is None:
            # json_schema mode without a schema would silently ignore a
            # server-returned ``info.structured``; fail loudly instead.
            raise OpenCodeError(
                ERROR_REQUEST_NOT_SUPPORTED,
                "OpenCodeServerBackend: json_schema mode requires a response_schema",
            )
        self.preflight()

        provider_id, model_id = _parse_model_ref(request.model_ref)

        # The request's model must be one of the role->model bindings this
        # backend was configured with (like LocalOpenAIBackend rejects a
        # model_ref that is not the model it actually serves). A request
        # outside the bindings fails loudly instead of being routed
        # somewhere unexpected.
        bound_models = set(self._cfg.model_bindings.values())
        if bound_models and request.model_ref not in bound_models:
            raise OpenCodeError(
                ERROR_REQUEST_NOT_SUPPORTED,
                f"OpenCodeServerBackend: model_ref {request.model_ref!r} is not "
                f"bound for any role (bindings: {sorted(bound_models)})",
            )
        self._check_provider_model(provider_id, model_id)

        started = time.perf_counter()
        transport_attempts = 0
        max_transport_attempts = self._cfg.http_retries + 1
        structured_attempts = 0
        max_structured_attempts = self._cfg.structured_output_retry_count + 1
        attempt_log: list[dict] = []

        while True:
            try:
                session_id = self._create_session(request.label)
            except OpenCodeError as exc:
                # Session creation (POST /session) or request-budget
                # admission failed after a successful preflight. This is a
                # failed remote completion call and must be journaled exactly
                # once (D1 acceptance §1). No session/request id exists yet,
                # so the record carries only the real error_class, the label
                # and the attempt entry — never fabricated ids or usage.
                attempt_log.append(
                    _attempt_entry(exc, exc.session_id, request.model_ref)
                )
                self._raise_final(exc, attempt_log, started, request)
            try:
                info, parts, status = self._post_message(
                    session_id, request, provider_id=provider_id, model_id=model_id
                )
            except OpenCodeError as exc:
                exc.session_id = session_id
                self._owned_sessions[session_id] = "failed"
                attempt_log.append(_attempt_entry(exc, session_id, request.model_ref))
                if (
                    exc.error_class in _RETRYABLE_ERROR_CLASSES
                    and transport_attempts < max_transport_attempts - 1
                ):
                    if not self._can_retry():
                        if not self._cfg.retain_failed_sessions:
                            self._delete_own_session(session_id)
                        self._raise_budget_exhausted(exc, attempt_log, started, request)
                    transport_attempts += 1
                    self._reserve_retry()
                    self._backoff(exc)
                    continue
                if not self._cfg.retain_failed_sessions:
                    self._delete_own_session(session_id)
                self._raise_final(exc, attempt_log, started, request)

            message_error = self._map_message_error(info, session_id=session_id)
            if message_error is not None:
                self._owned_sessions[session_id] = "failed"
                attempt_log.append(
                    _attempt_entry(message_error, session_id, request.model_ref)
                )
                if (
                    message_error.error_class in _STRUCTURED_RETRYABLE_ERROR_CLASSES
                    and self._cfg.structured_output_mode == "json_schema"
                    and structured_attempts < max_structured_attempts - 1
                ):
                    if not self._can_retry():
                        if not self._cfg.retain_failed_sessions:
                            self._delete_own_session(session_id)
                        self._raise_budget_exhausted(
                            message_error, attempt_log, started, request
                        )
                    structured_attempts += 1
                    self._reserve_retry()
                    continue
                if not self._cfg.retain_failed_sessions:
                    self._delete_own_session(session_id)
                self._raise_final(message_error, attempt_log, started, request)

            # Success path.
            self._owned_sessions[session_id] = "success"
            text = self._extract_text(parts)
            structured = info.get("structured")
            if self._cfg.structured_output_mode == "json_schema":
                if structured is None:
                    # Server claimed json_schema but returned no structured
                    # object; treat as a structured-output failure (bounded
                    # retry above, then structured_output_failed).
                    err = OpenCodeError(
                        ERROR_STRUCTURED_OUTPUT_FAILED,
                        "opencode server returned no structured output for "
                        "json_schema request",
                        session_id=session_id,
                        request_id=info.get("id"),
                    )
                    self._owned_sessions[session_id] = "failed"
                    attempt_log.append(_attempt_entry(err, session_id, request.model_ref))
                    if structured_attempts < max_structured_attempts - 1:
                        if not self._can_retry():
                            if not self._cfg.retain_failed_sessions:
                                self._delete_own_session(session_id)
                            self._raise_budget_exhausted(
                                err, attempt_log, started, request
                            )
                        structured_attempts += 1
                        self._reserve_retry()
                        continue
                    if not self._cfg.retain_failed_sessions:
                        self._delete_own_session(session_id)
                    self._raise_final(err, attempt_log, started, request)
                text = _canonical_structured_text(structured)

            request_id = info.get("id")
            finish_reason = info.get("finish")
            usage = _normalize_usage(info)
            wall = time.perf_counter() - started
            attempt_log.append(
                {
                    "session_id": session_id,
                    "request_id": request_id,
                    "error_class": None,
                    "http_status": status,
                    "model_ref": request.model_ref,
                }
            )

            response = CompletionResponse(
                text=text,
                structured=dict(structured) if isinstance(structured, Mapping) else None,
                provider=info.get("providerID") or provider_id,
                model=info.get("modelID") or model_id,
                finish_reason=finish_reason,
                usage=usage,
                wall_seconds=round(wall, 3),
                request_id=request_id,
                session_id=session_id,
                retry_count=transport_attempts + structured_attempts,
                raw_metadata={
                    "server_version": self._server_version,
                    "attempts": attempt_log,
                    "structured_output_mode": self._cfg.structured_output_mode,
                },
            )

            if not self._cfg.retain_success_sessions:
                self._delete_own_session(session_id)

            self._records.append(
                BackendCallRecord(
                    label=request.label,
                    model_ref=request.model_ref,
                    request_id=request_id,
                    session_id=session_id,
                    retry_count=response.retry_count,
                    finish_reason=finish_reason,
                    usage=usage,
                    wall_seconds=response.wall_seconds,
                    raw_metadata=response.raw_metadata,
                )
            )
            # D1: per-call usage write at completion (crash-safe: the call
            # is in usage.ndjson the moment it finishes, not at a phase
            # boundary). Local lifecycle calls never reach this backend.
            self._emit_usage(self._records[-1])
            return response

    def _raise_final(
        self,
        exc: OpenCodeError,
        attempt_log: list,
        started: float,
        request: CompletionRequest,
    ) -> NoReturn:
        """Record the failed call and raise the final normalized error."""
        self._record_failure(exc, attempt_log, started, request)
        raise exc

    def _raise_budget_exhausted(
        self,
        cause: OpenCodeError,
        attempt_log: list,
        started: float,
        request: CompletionRequest,
    ) -> NoReturn:
        """Record and raise an explicit operational budget-exhaustion error."""
        budget = self._cfg.remote_budget
        message = (
            "remote budget exhausted: "
            f"max_retry_requests_per_chapter={budget.max_retry_requests_per_chapter} "
            f"(last error: {cause.error_class})"
        )
        exc = OpenCodeError(
            ERROR_REMOTE_BUDGET_EXHAUSTED,
            message,
            session_id=cause.session_id,
            request_id=cause.request_id,
        )
        self._record_failure(exc, attempt_log, started, request)
        raise exc

    def _record_failure(
        self,
        exc: OpenCodeError,
        attempt_log: list,
        started: float,
        request: CompletionRequest,
    ) -> None:
        wall = time.perf_counter() - started
        self._records.append(
            BackendCallRecord(
                label=request.label,
                model_ref=request.model_ref,
                request_id=exc.request_id,
                session_id=exc.session_id,
                retry_count=len(attempt_log) - 1,
                finish_reason=None,
                usage={},
                wall_seconds=round(wall, 3),
                raw_metadata={
                    "error_class": exc.error_class,
                    "attempts": attempt_log,
                    "server_version": self._server_version,
                },
            )
        )
        # D1: failed calls are written to usage.ndjson too (per completed
        # call, crash-safe — same moment the failure is finalized).
        self._emit_usage(self._records[-1])

    def _backoff(self, exc: OpenCodeError) -> None:
        delay = exc.retry_after
        if delay is None or delay <= 0:
            delay = self._cfg.retry_delay_seconds
        self._consume_wait_budget(delay)
        time.sleep(delay)

    def close(self) -> None:
        # Idempotent. Closes the HTTP session and deletes only sessions this
        # backend created that the session policy permits deleting.
        if self._closed:
            return
        self._closed = True
        for session_id, outcome in list(self._owned_sessions.items()):
            deletable = (
                (outcome == "success" and not self._cfg.retain_success_sessions)
                or (outcome == "failed" and not self._cfg.retain_failed_sessions)
            )
            if deletable:
                self._delete_own_session(session_id)
        try:
            close = getattr(self._session, "close", None)
            if callable(close):
                close()
        except Exception as exc:  # pragma: no cover - defensive teardown
            LOG.warning(
                "OpenCodeServerBackend: error closing HTTP session: %s", exc
            )

    def set_usage_sink(self, sink: Any) -> None:
        """Attach a per-call usage sink (``UsageRecordWriter.write_call``).

        Called at the exact moment each call completes (success or final
        failure), so ``usage.ndjson`` is written per completed call — a
        crash inside a phase never loses already-completed calls. Each call
        is recorded exactly once, so a resumed run (a fresh backend) can
        never duplicate an already-journaled call.
        """
        self._usage_sink = sink

    def _emit_usage(self, record: BackendCallRecord) -> None:
        if self._usage_sink is not None:
            try:
                self._usage_sink(record)
            except Exception:  # noqa: BLE001 -- usage is diagnostics, never a gate
                LOG.warning(
                    "OpenCodeServerBackend: usage sink failed; disabling",
                    exc_info=True,
                )
                self._usage_sink = None

    def call_records(self) -> Sequence[BackendCallRecord]:
        return list(self._records)


def _parse_retry_after(resp: requests.Response) -> Optional[float]:
    """Parse ``Retry-After`` (integer seconds) from response headers."""
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _attempt_entry(
    exc: OpenCodeError, session_id: str, model_ref: str
) -> dict:
    return {
        "session_id": session_id,
        "request_id": exc.request_id,
        "error_class": exc.error_class,
        "http_status": exc.status_code,
        "model_ref": model_ref,
    }


def _canonical_structured_text(structured: Any) -> str:
    return json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "OPENCODE_SERVER_TRANSPORT_VERSION",
    "ENDPOINT_FAMILY_OPENCODE_HTTP",
    "OPENCODE_PINNED_SERVER_VERSION",
    "OPENCODE_SYSTEM_PROMPT_VERSION",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_DISABLED_TOOLS",
    "build_opencode_descriptor",
    "ERROR_TRANSPORT_TIMEOUT",
    "ERROR_TRANSPORT_NETWORK",
    "ERROR_PROVIDER_429",
    "ERROR_PROVIDER_5XX",
    "ERROR_PROVIDER_AUTH",
    "ERROR_PROVIDER_MODEL_UNAVAILABLE",
    "ERROR_STRUCTURED_OUTPUT_FAILED",
    "ERROR_INVALID_MODEL_OUTPUT",
    "ERROR_SEMANTIC_GATE_FAILED",
    "ERROR_SERVER_VERSION_UNSUPPORTED",
    "ERROR_REMOTE_BUDGET_EXHAUSTED",
    "ERROR_REQUEST_NOT_SUPPORTED",
    "OpenCodeError",
    "RemoteBudget",
    "OpenCodeServerBackendConfig",
    "OpenCodeServerBackend",
]
