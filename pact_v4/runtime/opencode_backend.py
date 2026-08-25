"""``OpenCodeServerBackend`` — remote model transport over ``opencode serve``.

A ``CompletionBackend`` implementation (V4 integration plan, PR 2 / Поток C,
``docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md`` §7, §10,
§13, §14.2) that talks to a *pre-started* ``opencode serve`` HTTP/OpenAPI
server directly. No TypeScript sidecar / Node SDK: the Python client speaks
the REST contract of the installed server.

Version policy (version-agnostic, owner 2026-08-14)
---------------------------------------------------

The adapter is **not** pinned to a specific opencode version. The
request/response contract was verified against three sources and re-checked
on each supported server line (1.4.7 in 2026-08-01, 1.18.18 in 2026-08-14):

* the generated SDK types at the tag
  (``packages/sdk/js/src/gen/types.gen.ts``);
* the server source at the tag
  (``packages/opencode/src/session/prompt.ts``, ``message-v2.ts``,
  ``llm.ts``);
* a live ``opencode serve`` probe (health/provider/agent/tool-ids).

The wire contract below is stable across both verified lines (the same
endpoints, the same body fields, the same response shape); the only
difference found on 1.18.18 is that the server schema no longer lists a
top-level ``reasoningEffort`` field (reasoning effort moved to the model
``variant``), and the Effect Struct decoder silently ignores unknown body
keys — so sending ``reasoningEffort`` is accepted but has no effect on
1.18.18 (verified empirically 2026-08-14: request accepted, response fine).

Contract facts used here:

* ``GET /global/health`` -> ``{"healthy": true, "version": "..."}``;
* ``GET /provider`` -> ``{all: [Provider], default: {...}, connected: [...]}``;
* ``POST /session`` ``{title?}`` -> ``Session{id, ...}``;
* ``DELETE /session/{id}`` -> ``boolean``;
* ``POST /session/{id}/message`` ``{model?, agent?, system?, tools?,
  format?, parts}`` -> ``{info: AssistantMessage, parts: Part[]}``;
* **output-budget quirk (AF, 2026-08-10, serve 1.4.7)**: a message body
  that carries ``system`` and/or ``tools`` was served with a default ~32k
  output budget, truncating whole-chapter generation reasoning at 32000
  tokens (``finish=length``, empty output). A body with only
  ``model``+``parts`` (+``reasoningEffort``) — the verbatim Gate 0 shape —
  was not capped (measured 55915 reasoning tokens with ``finish=stop``).
  The generation caller therefore sets
  ``CompletionRequest.omit_system_tools``; audit / repair / formatting keep
  ``system``+``tools`` (out of scope). The flag is harmless on server lines
  without the cap, so it is kept.
* ``GET /experimental/tool/ids`` -> ``string[]`` (used to build the
  all-tools-disabled map).

``json_schema`` structured output is sent via the message-body ``format``
field. The verified servers accept ``retryCount`` in ``format`` but perform
a single attempt (``StructuredOutputError`` with ``retries: 0`` on
failure), so this backend implements the bounded structured-output retry
itself (§7.4).

The version returned by ``GET /global/health`` is recorded in the backend
identity and the preflight report (so runs know what they ran on) but is
never a hard gate: a different server version logs a warning and proceeds.
The fake-server contract suite (``tests/pact_v4/runtime/``) is the gate —
after every opencode upgrade, run it against the new version.

Design rules (plan §5, §7, §10, §12)
-------------------------------------

* Read-only preflight before the first model call: health + version (log),
  provider connected, model exists, tool IDs for the disabled-tools map.
* One isolated session per work unit: ``session_scope=per_request``,
  ``context_reuse=false``, every message carries an explicit
  ``provider/model`` and ``tools`` (all disabled) — except generation
  requests (``omit_system_tools``), which drop ``system``/``tools`` from
  the body to escape the serve output-budget cap (see quirk above);
  ``close()`` deletes only
  sessions this backend created, and only when the session policy allows.
* ``BackendDescriptor`` includes everything that can change the model answer
  (model bindings, adapter/server contract version, endpoint family,
  agent/system identity, structured-output mode + schema version, effective
  options, retry policy) and excludes credentials and local paths.
* Per-request ``temperature``/``max_output_tokens`` are recorded in identity
  but *not* sent in the message body (the verified server lines have no
  per-request sampling fields; agent/model defaults apply). Non-empty
  ``request_options`` are rejected loudly rather than silently ignored —
  except the V4.1 ``reasoning`` option, which maps to the server's
  top-level ``reasoningEffort`` field (1=low, 2=medium, 3=high).
* Error normalization per §10; no silent model fallback; semantic
  ``passed=False`` is never retried as a transport error (the backend never
  interprets verdicts — it returns text/structured and the Pact layer gates).
"""
from __future__ import annotations

import hashlib
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
# when the request/response contract changes. The suffix reflects the wire
# contract generation, not a server pin (version-agnostic since 2026-08-14).
OPENCODE_SERVER_TRANSPORT_VERSION = "opencode-server-http/v1.4"

# Endpoint family discriminates request/response interpretation (and thus
# backend identity). The value matches the family string already used in
# tests/pact_v4/runtime/test_backend_protocol.py.
ENDPOINT_FAMILY_OPENCODE_HTTP = "opencode_http"

# Server version this adapter was developed and verified against. Since the
# version-agnostic decision (owner 2026-08-14) this is an informational
# default ("latest" = no pin; any running `opencode serve` is accepted and
# its version is logged into identity/preflight). A config may still set an
# explicit value for exact/compatible_minor logging.
OPENCODE_PINNED_SERVER_VERSION = "latest"

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
        if self.max_reported_cost is not None:
            import math
            try:
                v = float(self.max_reported_cost)
            except (TypeError, ValueError):
                raise ValueError("RemoteBudget: max_reported_cost must be a finite non-negative number") from None
            if not math.isfinite(v) or v < 0:
                raise ValueError("RemoteBudget: max_reported_cost must be a finite non-negative number")
            object.__setattr__(self, "max_reported_cost", v)


@dataclass(frozen=True)
class OpenCodeServerBackendConfig:
    """Identity-relevant settings of an ``OpenCodeServerBackend``.

    ``username``/``password`` may be provided directly or resolved from the
    named environment variables. They never enter the descriptor identity or
    ``public_record()``. ``base_url`` is the public endpoint of the
    pre-started ``opencode serve`` (plan §7.1).
    """

    base_url: str = "http://127.0.0.1:4096"
    server_version_policy: str = "compatible_minor"  # exact | compatible_minor (log-only)
    pinned_server_version: str = OPENCODE_PINNED_SERVER_VERSION  # "latest" = no pin
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
    # TIMEOUT-FIX (2026-08-13): whole-chapter generation with --reasoning 3
    # (high) takes up to ~10 min (run_remote_003: attempt 2 was cut at
    # exactly 600s; 001/002 took 7-9:45 min; 003 streamed 155k delta events
    # for 10+ min). Default raised 600 -> 900 (15 min) per OWNER DECISION
    # 2026-08-13 — 900s is headroom over the real 7-10 min, explicitly NOT
    # 2400 — so a long generation is NOT aborted by the transport. The value
    # is part of backend identity (build_opencode_descriptor), so a config
    # that changes it needs a fresh --out-dir/--run-label.
    timeout_seconds: float = 900.0
    http_retries: int = 2
    retry_delay_seconds: float = 5.0

    # Remote budgets (plan §10).
    remote_budget: RemoteBudget = field(default_factory=RemoteBudget)

    # Role -> provider/model bindings (part of backend identity).
    model_bindings: Mapping[str, str] = field(default_factory=dict)

    # PROVIDERS-REGISTRY (2026-08-14): the --reasoning level (1/2/3) ->
    # reasoningEffort map for the model(s) this backend serves, from the
    # provider registry's reasoning contract. None keeps the historical
    # transport default {1: low, 2: medium, 3: high} and is NOT serialized
    # into the descriptor (existing configs keep their exact identity).
    # When set, the wire value changes with the model contract (e.g.
    # deepseek-v4-flash {low, high, max}: 2 -> high) and the map IS part of
    # backend identity — a contract change invalidates cache/resume.
    reasoning_effort_map: Optional[Mapping[int, str]] = None

    # Remote default reasoning level (profile-bearing, identity-bearing).
    # Explicitly records the generation reasoning budget for this backend
    # (0=off baseline, 1=low, 2=medium, 3=high). Code default 0 is fallback
    # only when profile omits the field to preserve backward compatibility;
    # canonical remote profile MUST set it explicitly. Part of effective_options
    # identity so a policy change invalidates cache/resume.
    reasoning: Optional[int] = None

    # Effective sampling settings (plan §5.4). The verified server lines
    # cannot send these in the message body, but they belong in backend
    # identity so a change in requested sampling invalidates cache/resume
    # instead of silently reusing a candidate generated with different
    # settings. origin/dev/v4.1-reasoning-transport
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
        if self.reasoning_effort_map is not None:
            effort_map = dict(self.reasoning_effort_map)
            unknown = set(effort_map) - {1, 2, 3}
            if unknown:
                raise ValueError(
                    "OpenCodeServerBackendConfig: reasoning_effort_map keys must be "
                    f"--reasoning levels 1/2/3, got {sorted(unknown)}"
                )
            empty = [k for k, v in effort_map.items() if not isinstance(v, str) or not v]
            if empty:
                raise ValueError(
                    "OpenCodeServerBackendConfig: reasoning_effort_map values must be "
                    f"non-empty reasoningEffort strings (bad levels: {empty})"
                )
            object.__setattr__(self, "reasoning_effort_map", effort_map)
        if self.reasoning is not None:
            if isinstance(self.reasoning, bool) or not isinstance(self.reasoning, int):
                raise ValueError(
                    "OpenCodeServerBackendConfig: reasoning must be an integer 0-3, "
                    f"got {self.reasoning!r}"
                )
            if self.reasoning not in (0, 1, 2, 3):
                raise ValueError(
                    "OpenCodeServerBackendConfig: reasoning must be 0-3, "
                    f"got {self.reasoning!r}"
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
    *,
    observed_server_version: Optional[str] = None,
) -> BackendDescriptor:
    """Build the backend identity descriptor for an OpenCode config.

    Module-level (not a backend method) so config loaders can compute
    ``identity_hash`` without constructing a backend / HTTP session.
    Includes everything that can change the model answer (bindings,
    adapter/server contract version, endpoint family, agent/system
    identity, structured-output mode + schema version, effective options,
    retry policy) and excludes credentials (plan §5.4, §12).

    ``observed_server_version`` is runtime provenance (the version the
    connected server reported via health); it is persisted in
    ``public_record()`` but deliberately excluded from ``identity_hash``
    (version-agnostic policy, owner 2026-08-14: a server upgrade must not
    invalidate cache/resume identity). Config loaders that build the
    descriptor before any server contact leave it ``None``.
    """
    bindings = dict(cfg.model_bindings) or {"default": ""}
    system_prompt_hash = hashlib.sha256(cfg.system_prompt.encode("utf-8")).hexdigest()
    effective_options = {
        "server_version_policy": cfg.server_version_policy,
        "pinned_server_version": cfg.pinned_server_version,
        "agent": cfg.agent or "server-default",
        "system_prompt_version": cfg.system_prompt_version,
        "system_prompt_hash": system_prompt_hash,
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
            "max_reported_cost": cfg.remote_budget.max_reported_cost,
        },
    }
    # PROVIDERS-REGISTRY: the reasoning-effort map is serialized ONLY when
    # set — a None (registry not consulted) must keep the exact historical
    # descriptor identity, so an existing remote profile's cache/resume is
    # unaffected by this feature. When set, it changes the wire
    # reasoningEffort and therefore participates in identity.
    if cfg.reasoning_effort_map is not None:
        effective_options["reasoning_effort_map"] = dict(
            sorted(cfg.reasoning_effort_map.items())
        )
    if cfg.reasoning is not None:
        effective_options["reasoning"] = int(cfg.reasoning)
    return BackendDescriptor(
        kind=KIND_OPENCODE_SERVER,
        transport_version=cfg.transport_version,
        endpoint_family=cfg.endpoint_family,
        public_endpoint=cfg.base_url,
        model_bindings=bindings,
        effective_options=effective_options,
        observed_server_version=observed_server_version,
    )


def _major_minor(version: str) -> Tuple[int, int]:
    v = version.strip().lstrip("vV")
    pieces = v.split(".")
    try:
        return int(pieces[0]), int(pieces[1])
    except (IndexError, ValueError):
        raise ValueError(f"cannot parse version {version!r}")


def _is_variant_reasoning_server(version: Optional[str]) -> Optional[bool]:
    """True when the server moved reasoningEffort to the model variant.
    Returns None when version is missing or unparsable (unknown).
    """
    if not version:
        return None
    try:
        major, minor = _major_minor(version)
    except ValueError:
        return None
    return (major, minor) >= (1, 18)


def _version_compatible(
    server_version: str, *, policy: str, pinned: str
) -> bool:
    """Version-policy check against the adapter's reference version.

    Version-agnostic since 2026-08-14: the default pin is ``"latest"``
    (nothing to compare against -> always compatible). A config that pins
    an explicit version still gets exact/compatible_minor comparison, used
    only for logging (a mismatch never fails the preflight). An observed
    version that is not parseable as semver (``nightly``, ``dev``,
    ``canary``) is treated as "not compatible" so the callers log a
    warning and proceed — the observed server version is never a
    fail-path (review UPGRADE-SERVE-1.18 HIGH).
    """
    if pinned == "latest":
        return True
    if policy == "exact":
        return server_version == pinned
    try:
        return _major_minor(server_version) == _major_minor(pinned)
    except ValueError:
        # Non-semver observed or pinned version: cannot compare by minor.
        # Version-agnostic policy: warn-only, never a gate.
        return False


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
        self._reported_cost_total = 0.0

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
        unavailable, the provider is not connected, or the model is missing.
        A server version that differs from the configured pin is logged as a
        warning and does NOT fail (version-agnostic, owner 2026-08-14). No
        silent fallback.
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
        # The descriptor is cached; invalidate it so the next access picks
        # up the observed server version as runtime provenance (review
        # UPGRADE-SERVE-1.18 MEDIUM). identity_hash is unaffected.
        self._descriptor = None
        if not _version_compatible(
            version,
            policy=self._cfg.server_version_policy,
            pinned=self._cfg.pinned_server_version,
        ):
            # Version-agnostic (owner 2026-08-14): a different server version
            # is a warning, never a gate. The version is already recorded in
            # the preflight report / backend identity so runs know what they
            # ran on; the fake-server contract suite is the actual gate.
            LOG.warning(
                "OpenCodeServerBackend: server version %r does not match "
                "configured policy %r / pinned %r; proceeding version-agnostic",
                version,
                self._cfg.server_version_policy,
                self._cfg.pinned_server_version,
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
            "parts": [
                {"type": "text", "text": msg.content}
                for msg in request.messages
            ],
        }
        if not request.omit_system_tools:
            body["system"] = self._cfg.system_prompt
        if self._cfg.agent:
            body["agent"] = self._cfg.agent
        if self._cfg.tools_disabled and not request.omit_system_tools:
            body["tools"] = {tool_id: False for tool_id in self._tool_ids}
        # Output-budget fix (2026-08-20): pass the request's explicit token
        # budget to the relay so it does not apply its upstream default
        # (~32k), which truncated long whole-chapter generations at exactly
        # 32000 (finish=length) while the model itself (e.g. Muse) can emit
        # 49k+ tokens. Direct probe (owner 2026-08-20) confirmed Muse returns
        # 49k / finish=stop with no program limit, and ~32000 / finish=length
        # once the relay's default was hit. Both ``max_completion_tokens``
        # (OpenAI-style) and the legacy ``max_tokens`` are sent so either
        # server generation honours it. ``max_output_tokens`` is always > 0
        # (GenerationConfig default 70000; audit/repair carry their own).
        body["max_completion_tokens"] = int(request.max_output_tokens)
        body["max_tokens"] = int(request.max_output_tokens)
        if self._cfg.structured_output_mode == "json_schema" and request.response_schema is not None:
            body["format"] = {
                "type": "json_schema",
                "schema": dict(request.response_schema),
                "retryCount": self._cfg.structured_output_retry_count,
            }
        reasoning = request.request_options.get("reasoning")
        if reasoning:
            # V4.1: top-level reasoningEffort on POST /session/{id}/message
            # was honoured by serve 1.4.7 (empirically verified 2026-08-08:
            # high -> 23 reasoning tokens, absent -> 0); on 1.18.18 the field
            # is no longer in the request schema and is silently ignored
            # (Effect Struct strips unknown keys) while the request still
            # succeeds (verified 2026-08-14). Sending it stays harmless and
            # keeps 1.4.7-line behaviour, so it is kept. The GenerationParams
            # contract restricts reasoning to {0,1,2,3}, so an out-of-range
            # value here is a programming error — fail loudly instead of
            # silently dropping the budget.
            #
            # PROVIDERS-REGISTRY (2026-08-14): the level -> effort mapping is
            # taken from the config's reasoning_effort_map when set (the
            # provider registry's per-model reasoning contract, e.g.
            # deepseek-v4-flash {low, high, max}: 2 -> high); otherwise the
            # historical transport default {1: low, 2: medium, 3: high} is
            # used. The serve relay does NOT validate the value (any string
            # is accepted and proxied), so the contract is the authoritative
            # guide, not a server-side constraint.
            effort = (self._cfg.reasoning_effort_map or {1: "low", 2: "medium", 3: "high"}).get(
                reasoning
            )
            if effort is None:
                raise OpenCodeError(
                    ERROR_REQUEST_NOT_SUPPORTED,
                    "OpenCodeServerBackend: unsupported reasoning level "
                    f"{reasoning!r} in request_options (allowed: 1=low, "
                    "2=medium, 3=high)",
                )
            variant_mode = _is_variant_reasoning_server(self._server_version)
            if variant_mode is True:
                # 1.18+ moved reasoningEffort to the model variant (Effect Struct
                # silently ignores the old top-level key -> reasoning was silently lost).
                # Send the variant (effective contract) and keep reasoningEffort for
                # backward compatibility with 1.4 servers / existing test expectations
                # (1.18 ignores it, so it is harmless).
                body["model"]["variant"] = effort
                body["reasoningEffort"] = effort
            elif variant_mode is None:
                # Unknown/unparsable version (e.g. "v1.18.18", "nightly"):
                # fail-safe sends BOTH fields so reasoning is not lost regardless
                # of server line (either field ignored on the other line).
                body["model"]["variant"] = effort
                body["reasoningEffort"] = effort
            else:
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
        """Concatenate non-synthetic assistant ``text`` parts (verified lines)."""
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

    def _extract_reasoning(self, parts: Sequence[Mapping[str, Any]]) -> str:
        """Concatenate the model's thinking from message parts.

        Reasoning arrives either as dedicated ``type="reasoning"`` parts
        (``ReasoningPart`` — the canonical shape on the verified lines) or
        as synthetic text parts for providers that stream thinking through
        the text channel. Both must be excluded from ``_extract_text`` (that
        method keeps only non-synthetic ``text`` parts) and surfaced
        separately via ``raw_metadata["reasoning"]`` so audit/repair
        ``_reasoning.txt`` artifacts are not empty on the remote path.

        OpenAI (gpt-5.6-*, PROVIDERS-REGISTRY, verified on serve 1.18.18):
        the FULL reasoning is delivered encrypted —
        ``metadata.openai.reasoningEncryptedContent`` (the variant carries
        ``include: ["reasoning.encrypted_content"]``). This method therefore
        only ever sees the OPEN part.text (the summary / any unencrypted
        slice); the encrypted blob is NOT decrypted here — documented
        limitation, deliberately not fixed (the transport never held an
        OpenAI decryption key).
        """
        chunks = []
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            if part.get("ignored"):
                continue
            if part.get("type") == "reasoning":
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                continue
            if part.get("type") != "text":
                continue
            if not part.get("synthetic"):
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

    def _accumulate_reported_cost(self, info: Mapping[str, Any]) -> None:
        usage = _normalize_usage(info)
        if "reported_cost" in usage:
            import math
            try:
                v = float(usage["reported_cost"])
            except (TypeError, ValueError):
                return
            if not math.isfinite(v) or v < 0:
                return
            self._reported_cost_total += v

    def _check_budget_request(self) -> None:
        if self._request_count >= self._cfg.remote_budget.max_requests_per_chapter:
            raise OpenCodeError(
                ERROR_REMOTE_BUDGET_EXHAUSTED,
                "remote budget exhausted: "
                f"max_requests_per_chapter={self._cfg.remote_budget.max_requests_per_chapter}",
            )
        max_cost = self._cfg.remote_budget.max_reported_cost
        if max_cost is not None:
            import math
            # NaN/inf already rejected at config validation; total is kept finite.
            if not math.isfinite(self._reported_cost_total):
                self._reported_cost_total = 0.0
            if self._reported_cost_total >= float(max_cost):
                raise OpenCodeError(
                    ERROR_REMOTE_BUDGET_EXHAUSTED,
                    "remote budget exhausted: "
                    f"max_reported_cost={max_cost} (reported {self._reported_cost_total})",
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
        # Runtime provenance: the observed server version (health) is
        # recorded in the descriptor's public record once preflight has
        # run (review UPGRADE-SERVE-1.18 MEDIUM). Never affects
        # identity_hash (version-agnostic).
        return build_opencode_descriptor(
            self._cfg, observed_server_version=self._server_version
        )

    # v41-runtime-efficiency 4.1: complete() split into four steps for
    # testability while preserving exact retry/session/logging behavior.
    def _ensure_session(self, request: CompletionRequest) -> Tuple[str, str]:
        """Admission checks + preflight; return (provider_id, model_id)."""
        if self._closed:
            raise CompletionError(
                "OpenCodeServerBackend: backend is closed; cannot complete a request"
            )
        if request.request_options:
            unsupported = set(request.request_options) - {"reasoning"}
            if unsupported:
                raise OpenCodeError(
                    ERROR_REQUEST_NOT_SUPPORTED,
                    "OpenCodeServerBackend: request_options are not supported by "
                    f"{OPENCODE_SERVER_TRANSPORT_VERSION} (got {sorted(request.request_options)})",
                )
        if self._cfg.structured_output_mode == "json_schema" and request.response_schema is None:
            raise OpenCodeError(
                ERROR_REQUEST_NOT_SUPPORTED,
                "OpenCodeServerBackend: json_schema mode requires a response_schema",
            )
        self.preflight()
        provider_id, model_id = _parse_model_ref(request.model_ref)
        bound_models = set(self._cfg.model_bindings.values())
        if bound_models and request.model_ref not in bound_models:
            raise OpenCodeError(
                ERROR_REQUEST_NOT_SUPPORTED,
                f"OpenCodeServerBackend: model_ref {request.model_ref!r} is not "
                f"bound for any role (bindings: {sorted(bound_models)})",
            )
        self._check_provider_model(provider_id, model_id)
        return provider_id, model_id

    def _normalize(self, info: Mapping[str, Any], parts: Sequence[Mapping[str, Any]], request: CompletionRequest) -> Tuple[str, str, Any]:
        """Extract text/reasoning/structured and deliver reasoning sink (best-effort)."""
        text = self._extract_text(parts)
        reasoning = self._extract_reasoning(parts)
        if request.on_reasoning_chunk is not None and reasoning:
            try:
                request.on_reasoning_chunk(reasoning)
            except Exception:  # noqa: BLE001
                LOG.warning(
                    "OpenCodeServerBackend: on_reasoning_chunk callback "
                    "raised; reasoning delivery is best-effort",
                    exc_info=True,
                )
        structured = info.get("structured")
        if self._cfg.structured_output_mode == "json_schema" and structured is not None:
            text = _canonical_structured_text(structured)
        return text, reasoning, structured

    def _record_cost(self, info: Mapping[str, Any]) -> Mapping[str, Any]:
        """Normalize usage and accumulate reported cost; return usage dict."""
        usage = _normalize_usage(info)
        self._accumulate_reported_cost(info)
        return usage

    def _retry_loop(
        self,
        request: CompletionRequest,
        provider_id: str,
        model_id: str,
        started: float,
        attempt_log: list,
        max_transport_attempts: int,
        max_structured_attempts: int,
    ) -> Tuple[Mapping[str, Any], Sequence[Mapping[str, Any]], int, str, int, int]:
        """Core retry loop for one completion; returns (info, parts, status, session_id, transport_attempts, structured_attempts).

        Mirrors the pre-v41 complete() while loop: session creation,
        message POST, message-level error mapping, transport/structured
        retries with backoff and budget checks. On success returns the
        successful attempt; on terminal failure it raises via
        _raise_final/_raise_budget_exhausted (never returns).
        """
        transport_attempts = 0
        structured_attempts = 0
        while True:
            try:
                session_id = self._create_session(request.label)
            except OpenCodeError as exc:
                attempt_log.append(_attempt_entry(exc, exc.session_id, request.model_ref))
                self._raise_final(exc, attempt_log, started, request)
            try:
                info, parts, status = self._post_message(
                    session_id, request, provider_id=provider_id, model_id=model_id
                )
            except OpenCodeError as exc:
                exc.session_id = session_id
                self._owned_sessions[session_id] = "failed"
                attempt_log.append(_attempt_entry(exc, session_id, request.model_ref))
                if exc.error_class in _RETRYABLE_ERROR_CLASSES and transport_attempts < max_transport_attempts - 1:
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
                self._accumulate_reported_cost(info)
                attempt_log.append(_attempt_entry(message_error, session_id, request.model_ref))
                structured_retryable = (
                    message_error.error_class in _STRUCTURED_RETRYABLE_ERROR_CLASSES
                    and self._cfg.structured_output_mode == "json_schema"
                    and structured_attempts < max_structured_attempts - 1
                )
                transport_retryable = (
                    message_error.error_class in _RETRYABLE_ERROR_CLASSES
                    and transport_attempts < max_transport_attempts - 1
                )
                if structured_retryable or transport_retryable:
                    if not self._can_retry():
                        if not self._cfg.retain_failed_sessions:
                            self._delete_own_session(session_id)
                        self._raise_budget_exhausted(message_error, attempt_log, started, request)
                    if transport_retryable:
                        transport_attempts += 1
                        self._reserve_retry()
                        self._backoff(message_error)
                    else:
                        structured_attempts += 1
                        self._reserve_retry()
                    continue
                if not self._cfg.retain_failed_sessions:
                    self._delete_own_session(session_id)
                self._raise_final(message_error, attempt_log, started, request)
            structured = info.get("structured")
            if self._cfg.structured_output_mode == "json_schema" and structured is None:
                self._accumulate_reported_cost(info)
                err = OpenCodeError(
                    ERROR_STRUCTURED_OUTPUT_FAILED,
                    "opencode server returned no structured output for json_schema request",
                    session_id=session_id,
                    request_id=info.get("id"),
                )
                self._owned_sessions[session_id] = "failed"
                attempt_log.append(_attempt_entry(err, session_id, request.model_ref))
                if structured_attempts < max_structured_attempts - 1:
                    if not self._can_retry():
                        if not self._cfg.retain_failed_sessions:
                            self._delete_own_session(session_id)
                        self._raise_budget_exhausted(err, attempt_log, started, request)
                    structured_attempts += 1
                    self._reserve_retry()
                    continue
                if not self._cfg.retain_failed_sessions:
                    self._delete_own_session(session_id)
                self._raise_final(err, attempt_log, started, request)
            return info, parts, status, session_id, transport_attempts, structured_attempts

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        provider_id, model_id = self._ensure_session(request)

        started = time.perf_counter()
        max_transport_attempts = self._cfg.http_retries + 1
        max_structured_attempts = self._cfg.structured_output_retry_count + 1
        attempt_log: list[dict] = []
        info, parts, status, session_id, transport_attempts, structured_attempts = self._retry_loop(
            request, provider_id, model_id, started, attempt_log, max_transport_attempts, max_structured_attempts
        )
        self._owned_sessions[session_id] = "success"
        text, reasoning, structured = self._normalize(info, parts, request)
        usage = self._record_cost(info)
        request_id = info.get("id")
        finish_reason = info.get("finish")
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
                "reasoning": reasoning,
                "reasoning_streamed": False,
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
