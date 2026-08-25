"""Tagged runtime backend configs (V4 C2 / PR 3 of the OpenCode plan).

Per ``docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md`` §9.2
the strict driver's backend identity is no longer a single local-only
``StrictBackendConfig``. Three tagged configs share one interface:

* ``LocalLlamaBackendConfig`` -- the old ``StrictBackendConfig`` content,
  now also able to build a ``BackendDescriptor`` and a runtime
  coordinator;
* ``OpenCodeBackendConfig`` -- a pre-started (``external``) or
  self-managed (``managed``) ``opencode serve`` profile;
* ``CompositeBackendConfig`` -- a role -> backend routing map over the
  first two.

Common interface (``BackendRuntimeConfig``):

* ``identity_hash`` -- everything that can change the model answer,
  never credentials (API key rotation does not invalidate resume);
* ``public_record()`` -- sanitized, safe for artifacts/logs;
* ``build_descriptor()`` -- the ``BackendDescriptor``;
* ``build_runtime(log_dir=...)`` -- the ``RuntimeCoordinator`` (starting a
  managed server when configured);
* ``config_profile_name()`` -- a stable label for the run's
  ``ConfigArtifact`` profile;
* ``acceptable_identity_hashes()`` -- hashes a stored journal/cache may
  carry and still be resumed against (local keeps a legacy hash so old
  v1 journals written by ``StrictBackendConfig`` still resume).

Secrets are only ever *references* (env var names) in configs; values are
resolved at backend construction from the environment and never persisted
(plan §12).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlsplit

from pact_v4.phase1.models import canonical_json_hash
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.backend_protocol import (
    KIND_COMPOSITE,
    KIND_LOCAL_LLAMA,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
    ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS,
)
from pact_v4.runtime.json_resilience import JsonRetryPolicy
from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.opencode_backend import (
    OPENCODE_PINNED_SERVER_VERSION,
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
    RemoteBudget,
    build_opencode_descriptor,
)
from pact_v4.runtime.opencode_server_lifecycle import (
    DEFAULT_HOSTNAME,
    ManagedServerSpec,
    OpenCodeServerProcess,
)
from pact_v4.runtime.runtime_coordinator import (
    CompositeRuntimeCoordinator,
    LocalLifecycleCoordinator,
    RemoteRuntimeCoordinator,
    RuntimeCoordinator,
)

# Adapter/transport version of the local-llama descriptor. Bump when the
# request/response contract of the local OpenAI-compatible adapter changes.
LOCAL_LLAMA_TRANSPORT_VERSION = "local-llama/v1"

# Composite endpoint family (routes by role->backend map; part of identity).
ENDPOINT_FAMILY_COMPOSITE = "pact_composite"
COMPOSITE_TRANSPORT_VERSION = "composite/v1"

# Role names used in model_bindings (config aliases, plan §8).
ROLE_GENERATOR = "generator"
ROLE_FIDELITY_REVIEWER = "fidelity_reviewer"
ROLE_RUSSIAN_SELECTOR = "russian_selector"
ROLE_QWEN_AUDIT = "qwen_audit"
ROLE_GEMMA_AUDIT = "gemma_audit"
# Phase 5 formatting fallback (B3). Configs bind it explicitly when they
# want a dedicated model for span restoration; otherwise it falls back to
# the repair/generator binding.
ROLE_FORMATTING = "formatting"
# Phase 4A region repair (B2). Falls back to the `generator` binding when
# unset (backend_role_adapters resolves ("repair", "generator")).
ROLE_REPAIR = "repair"
# B1.2 entity-extractor role (ChapterEntityContext prepass). Resolved by
# BackendEntityExtractor via a runtime _EntityRoleView when the descriptor
# lacks it; the providers registry maps it explicitly under --reviewer so a
# reviewer model can serve every audit role.
ROLE_ENTITY_EXTRACTOR = "entity_extractor"

# Roles bound by the --translator CLI flag (owner decision 2026-08-14):
# generation + region repair (repair falls back to generator anyway).
TRANSLATOR_ROLES = (ROLE_GENERATOR, ROLE_REPAIR)

# Roles bound by the --reviewer CLI flag: every audit role (Qwen audit,
# fidelity reviewer, Russian selector, entity extractor). Gemma audit is
# NOT included — it stays on the local/generator model by design.
REVIEWER_ROLES = (
    ROLE_QWEN_AUDIT,
    ROLE_FIDELITY_REVIEWER,
    ROLE_RUSSIAN_SELECTOR,
    ROLE_ENTITY_EXTRACTOR,
)


class BackendRuntimeConfig(Protocol):
    """Common interface of the tagged backend configs."""

    @property
    def identity_hash(self) -> str: ...

    def public_record(self) -> Mapping[str, Any]: ...

    def build_descriptor(self) -> BackendDescriptor: ...

    def build_runtime(
        self, *, log_dir: Optional[Path] = None
    ) -> RuntimeCoordinator: ...

    def config_profile_name(self) -> str: ...

    def acceptable_identity_hashes(self) -> Sequence[str]: ...


def _parse_url_port(url: str) -> Optional[int]:
    """Port from ``http://host:port/...`` (None when absent/unparseable)."""
    if not url:
        return None
    raw = url if "://" in url else f"http://{url}"
    try:
        return urlsplit(raw).port
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Local llama-server
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalLlamaBackendConfig:
    """Fixed identity for the llama-server backend + per-model server args.

    Deliberately mirrors the SYCL profile validated in Measurement 2
    (``V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md``) and is the exact
    content of the historical ``StrictBackendConfig`` (now an alias), so
    old local run configs behave identically.
    """

    exe: Path
    device: str
    host: str
    model_paths: Mapping[str, Path]
    model_names: Mapping[str, str]
    server_args: Mapping[str, List[str]]
    port: int = 8093
    startup_timeout: float = 240.0
    unload_timeout: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_paths", dict(self.model_paths))
        object.__setattr__(self, "model_names", dict(self.model_names))
        server_args = {k: list(v) for k, v in self.server_args.items()}
        # server_args are handed verbatim to ``subprocess.Popen``, so a
        # non-string value would fail deep inside ``list2cmdline`` with an
        # unreadable ``TypeError``. The most common way in is YAML 1.1 bool
        # words (`on`/`off`/`yes`/`no`/`true`/`false` parsed bare) -- refuse
        # here with a message that says so, instead of after the model server
        # was about to start.
        bad = {k: [a for a in v if not isinstance(a, str)] for k, v in server_args.items()}
        bad = {k: v for k, v in bad.items() if v}
        if bad:
            raise ValueError(
                "server_args must contain only strings, got "
                + "; ".join(f"{k}: {v!r}" for k, v in sorted(bad.items()))
                + " (quote YAML 1.1 bool words like `on`/`off`/`yes`/`no` in the "
                "runtime config)"
            )
        object.__setattr__(self, "server_args", server_args)

    def _role_bindings(self) -> Dict[str, str]:
        names = self.model_names
        bindings: Dict[str, str] = {}
        if "gemma" in names:
            bindings[ROLE_GENERATOR] = names["gemma"]
            bindings[ROLE_RUSSIAN_SELECTOR] = names["gemma"]
            bindings[ROLE_GEMMA_AUDIT] = names["gemma"]
        if "qwen" in names:
            bindings[ROLE_FIDELITY_REVIEWER] = names["qwen"]
            bindings[ROLE_QWEN_AUDIT] = names["qwen"]
        if not bindings and names:
            bindings["default"] = next(iter(names.values()))
        return bindings

    @property
    def identity_hash(self) -> str:
        """Backend descriptor identity (superset of the legacy hash)."""
        return self.build_descriptor().identity_hash

    @property
    def legacy_identity_hash(self) -> str:
        """The historical ``StrictBackendConfig.identity_hash``.

        Kept so journals/caches written before C2 still resume.
        """
        return canonical_json_hash({
            "exe": str(self.exe), "device": self.device,
            "model_paths": {k: str(v) for k, v in sorted(self.model_paths.items())},
            "model_names": dict(sorted(self.model_names.items())),
            "server_args": {k: list(v) for k, v in sorted(self.server_args.items())},
        })

    def build_descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            kind=KIND_LOCAL_LLAMA,
            transport_version=LOCAL_LLAMA_TRANSPORT_VERSION,
            endpoint_family=ENDPOINT_FAMILY_OPENAI_CHAT_COMPLETIONS,
            public_endpoint=f"http://{self.host}:{self.port}",
            model_bindings=self._role_bindings(),
            effective_options={
                "exe": str(self.exe),
                "device": self.device,
                "model_paths": {k: str(v) for k, v in sorted(self.model_paths.items())},
                "model_names": dict(sorted(self.model_names.items())),
                "server_args": {k: list(v) for k, v in sorted(self.server_args.items())},
                "structured_output": {
                    "mode": "json_object",
                    "schema_version": "pact-json-object/v1",
                },
            },
        )

    def public_record(self) -> Mapping[str, Any]:
        return self.build_descriptor().public_record()

    def config_profile_name(self) -> str:
        return self.model_names.get("gemma") or next(
            iter(self.model_names.values()), "local_llama"
        )

    def acceptable_identity_hashes(self) -> Sequence[str]:
        hashes = [self.identity_hash]
        if self.legacy_identity_hash not in hashes:
            hashes.append(self.legacy_identity_hash)
        return hashes

    def build_runtime(
        self, *, log_dir: Optional[Path] = None
    ) -> RuntimeCoordinator:
        from pact_v4.runtime.model_lifecycle import LifecycleAdapter

        log_dir = log_dir or (Path.cwd() / "v4_server_logs")
        adapter = LifecycleAdapter(
            self.exe, self.device, self.host, self.port, log_dir,
            self.model_paths,
            startup_timeout=self.startup_timeout, unload_timeout=self.unload_timeout,
        )
        router = ModelRouter(
            adapter,
            role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
            role_args=dict(self.server_args),
        )
        return LocalLifecycleCoordinator(router, descriptor=self.build_descriptor())


# The historical name is preserved as an alias so existing imports/tests
# keep working (plan §9.2).
StrictBackendConfig = LocalLlamaBackendConfig


# ---------------------------------------------------------------------------
# OpenCode server
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenCodeBackendConfig:
    """Runtime-level tagged config for an ``opencode serve`` profile.

    ``server`` carries the transport settings (including the role ->
    ``provider/model`` bindings and the structured-output/retry policy that
    belong in backend identity). ``server_mode``:

    * ``external`` -- a pre-started server (C1 style); the backend just
      talks to ``server.base_url``;
    * ``managed`` -- Pact starts its own ``opencode serve`` subprocess on
      a dedicated port with ephemeral basic-auth credentials (DECISIONS
      2026-08-01), health-waits, and stops only that process on
      ``close()``.

    In managed mode ``server.base_url`` must agree with the managed port so
    backend identity and the actual endpoint stay consistent.
    """

    server: OpenCodeServerBackendConfig
    server_mode: str = "external"
    managed: Optional[ManagedServerSpec] = None
    runtime_name: str = "opencode"

    def __post_init__(self) -> None:
        if self.server_mode not in ("managed", "external"):
            raise ValueError(
                f"OpenCodeBackendConfig: unknown server_mode {self.server_mode!r} "
                "(expected 'managed' or 'external')"
            )
        if self.server_mode == "managed":
            if self.managed is None:
                object.__setattr__(self, "managed", self._default_managed_spec())
            else:
                port = _parse_url_port(self.server.base_url)
                if port is not None and port != self.managed.port:
                    raise ValueError(
                        "OpenCodeBackendConfig: server.base_url port "
                        f"{port!r} does not match managed port "
                        f"{self.managed.port!r}; identity and endpoint must agree"
                    )

    def _default_managed_spec(self) -> ManagedServerSpec:
        port = _parse_url_port(self.server.base_url) or 4096
        return ManagedServerSpec(
            hostname=DEFAULT_HOSTNAME,
            port=port,
            pinned_server_version=self.server.pinned_server_version,
            server_version_policy=self.server.server_version_policy,
        )

    @property
    def identity_hash(self) -> str:
        return build_opencode_descriptor(self.server).identity_hash

    def build_descriptor(self) -> BackendDescriptor:
        return build_opencode_descriptor(self.server)

    def public_record(self) -> Mapping[str, Any]:
        record = dict(build_opencode_descriptor(self.server).public_record())
        record["server_mode"] = self.server_mode
        return record

    def config_profile_name(self) -> str:
        bindings = self.server.model_bindings or {}
        return (
            bindings.get(ROLE_GENERATOR)
            or bindings.get("default")
            or "opencode"
        )

    def acceptable_identity_hashes(self) -> Sequence[str]:
        return [self.identity_hash]

    def build_runtime(
        self, *, log_dir: Optional[Path] = None
    ) -> RuntimeCoordinator:
        server_cfg = self.server
        managed_proc: Optional[OpenCodeServerProcess] = None
        if self.server_mode == "managed":
            spec = self.managed or self._default_managed_spec()
            managed_proc = OpenCodeServerProcess(spec, log_dir=log_dir)
            managed_proc.start()
            username, password = managed_proc.credentials
            server_cfg = replace(
                server_cfg,
                base_url=managed_proc.base_url,
                username=username,
                password=password,
            )
        backend = OpenCodeServerBackend(config=server_cfg)
        runtime = RemoteRuntimeCoordinator(backend)
        if managed_proc is not None:
            runtime.add_cleanup(managed_proc.close)
        return runtime


# ---------------------------------------------------------------------------
# Local routing backend (used inside a composite profile)
# ---------------------------------------------------------------------------


class LocalRoutingBackend:
    """Lifecycle-aware ``CompletionBackend`` over a local router.

    Routes a local ``model_ref`` (a model name from the config's
    ``model_names``) to the single-resident router, ensuring the right
    model is resident before delegating to a ``LocalOpenAIBackend`` pointed
    at the router's base URL. Used by the composite profile so the same
    backend-role adapters can drive local and remote models.
    """

    def __init__(
        self,
        router: ModelRouter,
        config: LocalLlamaBackendConfig,
    ) -> None:
        self._router = router
        self._cfg = config
        self._ref_to_key = {
            name: key for key, name in config.model_names.items()
        }
        self._backends: Dict[str, LocalOpenAIBackend] = {}
        # MONITOR-V2 (2.1): per-call usage sink forwarded to every
        # LocalOpenAIBackend this routing backend creates (existing and
        # future), so local llama-server calls land in usage.ndjson.
        self._usage_sink: Optional[Any] = None

    def set_usage_sink(self, sink: Any) -> None:
        """Forward a per-call usage sink to every owned local backend.

        Called by the coordinator's ``set_usage_writer`` (MONITOR-V2 2.3):
        existing ``LocalOpenAIBackend`` instances get the sink immediately,
        and any backend created later by ``complete()`` receives it too.
        """
        self._usage_sink = sink
        for backend in self._backends.values():
            backend.set_usage_sink(sink)

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._cfg.build_descriptor()

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        key = self._ref_to_key.get(request.model_ref)
        if key is None:
            raise CompletionError(
                f"LocalRoutingBackend: model_ref {request.model_ref!r} is not a "
                f"local model (known: {sorted(self._ref_to_key)})"
            )
        self._router.ensure_resident(key)
        backend = self._backends.get(request.model_ref)
        if backend is None:
            api_config = ApiClientConfig(
                chat_url=f"{self._router.base_url}/v1/chat/completions",
                model=request.model_ref,
                timeout_seconds=1800.0,
                context_size=32768,
                temperature=request.temperature,
            )
            backend = LocalOpenAIBackend(api=ApiClient(api_config, name=request.label))
            if self._usage_sink is not None:
                backend.set_usage_sink(self._usage_sink)
            self._backends[request.model_ref] = backend
        return backend.complete(request)

    def close(self) -> None:
        # Close the per-model HTTP adapters first; the router's resident
        # model is released afterwards by the coordinator's close(). Order
        # matters: never release the router before its adapters are done.
        for backend in list(self._backends.values()):
            backend.close()

    def call_records(self) -> Sequence[Any]:
        records: list = []
        for backend in self._backends.values():
            records.extend(backend.call_records())
        return records


# ---------------------------------------------------------------------------
# Composite backend (role -> backend routing)
# ---------------------------------------------------------------------------


class CompositeCompletionBackend:
    """One ``CompletionBackend`` that routes each request to its backend.

    Routing key is ``request.model_ref``: the union of sub-backend
    bindings maps each model reference to the backend that serves it, so
    the backend-role adapters stay unchanged. ``close()`` closes only the
    sub-backends this composite owns.
    """

    def __init__(
        self,
        sub_backends: Mapping[str, CompletionBackend],
        descriptor: BackendDescriptor,
    ) -> None:
        self._sub = dict(sub_backends)
        self._descriptor = descriptor
        self._ref_to_name: Dict[str, str] = {}
        # Role-authoritative routing (RV t_7b26974e HIGH): a model ref is
        # routed to the sub-backend that actually serves the role(s) it is
        # bound to, resolved exactly like ``build_descriptor``/``apply_role_models``
        # — explicit routing map first, then the first sub-backend declaring
        # the role, then the documented runtime fallback (``repair`` ->
        # ``generator``, ``entity_extractor`` -> ``qwen_audit``). Without
        # this a composite whose sub-backends declare the same role (or
        # share a model ref) routed last-wins, so ``--translator``/
        # ``--reviewer`` overrides could be served by a different concrete
        # backend than the one the descriptor advertised. Refs that no
        # declared role resolves (e.g. a lone ``default`` binding) keep the
        # historical last-wins union, so non-ambiguous composites route
        # exactly as before.
        routing = dict(descriptor.effective_options.get("routing") or {})
        refs_by_backend = {
            name: dict(backend.descriptor.model_bindings or {})
            for name, backend in self._sub.items()
        }
        # Fail-closed on cross-role model_ref collisions (RV t_edb1033a):
        # when the same ref is bound to roles served by DIFFERENT concrete
        # backends, routing by ref alone cannot be coherent — the composite
        # must not be constructed at all. Never silently route one role's
        # requests to another role's backend (the historical sorted-role
        # last-wins pick).
        ambiguity = _composite_ref_ambiguity(routing, refs_by_backend)
        if ambiguity:
            raise ValueError(
                "CompositeCompletionBackend: "
                + _composite_ambiguity_message(ambiguity)
            )
        assigned: Dict[str, str] = {}
        for role in sorted({r for mb in refs_by_backend.values() for r in mb}):
            backend_name = _resolve_role_backend(routing, refs_by_backend, role)
            if backend_name is None or backend_name not in refs_by_backend:
                continue
            ref = refs_by_backend[backend_name].get(role)
            if ref:
                assigned[ref] = backend_name
        self._ref_to_name.update(assigned)
        for name, backend in self._sub.items():
            bindings = backend.descriptor.model_bindings or {}
            for ref in set(bindings.values()):
                if ref and ref not in self._ref_to_name:
                    self._ref_to_name[ref] = name

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    def observed_server_version(self) -> Optional[str]:
        """The observed OpenCode health version of the sub-backend, if any.

        Composite shape today is at most one remote OpenCode sub-backend
        (1-local + 1-remote, per ``CompositeBackendConfig.build_runtime``
        docs), so the first non-``None`` observed version among the
        sub-backends is authoritative for the composite descriptor. Local
        sub-backends never observe a server version (their descriptors
        carry ``None``).
        """
        for backend in self._sub.values():
            version = getattr(
                getattr(backend, "descriptor", None),
                "observed_server_version",
                None,
            )
            if version:
                return version
        return None

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        name = self._ref_to_name.get(request.model_ref)
        if name is None:
            raise CompletionError(
                "CompositeCompletionBackend: no backend serves model_ref "
                f"{request.model_ref!r} (known: {sorted(self._ref_to_name)})"
            )
        return self._sub[name].complete(request)

    def serving_backend(self, model_ref: str) -> Optional[CompletionBackend]:
        """The sub-backend that would serve ``model_ref`` (or ``None``).

        V4.1 A2: used to decide whether a request's reasoning travels via
        ``request_options`` (remote/OpenCode) or via the local server args
        (``--reasoning-budget``) — the decision must follow the concrete
        transport that will actually execute the call.
        """
        name = self._ref_to_name.get(model_ref)
        if name is None:
            return None
        return self._sub[name]

    def close(self) -> None:
        for backend in self._sub.values():
            backend.close()

    def call_records(self) -> Sequence[Any]:
        records: list = []
        for backend in self._sub.values():
            records.extend(backend.call_records())
        return records


@dataclass(frozen=True)
class CompositeBackendConfig:
    """Tagged config routing roles across local/remote sub-backends.

    ``role_backend_map`` maps each role name (``generator``,
    ``fidelity_reviewer``, ``russian_selector``, ``qwen_audit``,
    ``gemma_audit``) to a sub-backend name in ``backends``. The routing
    map is part of ``identity_hash``, so a different routing map can never
    reuse another profile's artifacts (plan §14.4).
    """

    backends: Mapping[str, BackendRuntimeConfig]
    role_backend_map: Mapping[str, str]
    runtime_name: str = "composite"

    def __post_init__(self) -> None:
        object.__setattr__(self, "backends", dict(self.backends))
        object.__setattr__(self, "role_backend_map", dict(self.role_backend_map))
        if not self.backends:
            raise ValueError("CompositeBackendConfig: at least one backend required")
        for role, name in self.role_backend_map.items():
            if name not in self.backends:
                raise ValueError(
                    f"CompositeBackendConfig: role {role!r} routes to unknown "
                    f"backend {name!r}"
                )

    @property
    def identity_hash(self) -> str:
        return self.build_descriptor().identity_hash

    def build_descriptor(self) -> BackendDescriptor:
        # Role-authoritative flattening (RV t_7b26974e HIGH): each role's
        # binding comes from the sub-backend that actually serves the role
        # (explicit role_backend_map first, then the first sub-backend
        # declaring it, then the documented runtime fallback — the same
        # resolution ``_resolve_role_backend`` applies at request time), so
        # the descriptor the role adapters read never advertises a ref that
        # routes to a different concrete backend than the role map selects.
        # For non-ambiguous composites (every role declared by exactly one
        # sub-backend) this is byte-identical to the historical first-wins
        # flatten.
        refs_by_backend = {
            name: (cfg.build_descriptor().model_bindings or {})
            for name, cfg in self.backends.items()
        }
        # Fail-closed on cross-role model_ref collisions (RV t_edb1033a):
        # when the same ref is bound to roles served by DIFFERENT concrete
        # backends, the composite cannot serve that ref coherently (requests
        # carry only the ref, never the role), so the descriptor must not be
        # built at all — the config is rejected loudly instead of advertising
        # a ref whose concrete routing silently picks one role's backend.
        ambiguity = _composite_ref_ambiguity(self.role_backend_map, refs_by_backend)
        if ambiguity:
            raise ValueError(
                "CompositeBackendConfig: " + _composite_ambiguity_message(ambiguity)
            )
        bindings: Dict[str, str] = {}
        for role in sorted({r for mb in refs_by_backend.values() for r in mb}):
            backend_name = _resolve_role_backend(
                self.role_backend_map, refs_by_backend, role
            )
            if backend_name is None or backend_name not in refs_by_backend:
                continue
            ref = refs_by_backend[backend_name].get(role)
            if ref:
                bindings[role] = ref
        return BackendDescriptor(
            kind=KIND_COMPOSITE,
            transport_version=COMPOSITE_TRANSPORT_VERSION,
            endpoint_family=ENDPOINT_FAMILY_COMPOSITE,
            public_endpoint="",
            model_bindings=bindings,
            effective_options={
                "routing": dict(sorted(self.role_backend_map.items())),
                "backends": {
                    name: cfg.identity_hash
                    for name, cfg in sorted(self.backends.items())
                },
            },
        )

    def public_record(self) -> Mapping[str, Any]:
        return {
            **self.build_descriptor().public_record(),
            "role_backend_map": dict(sorted(self.role_backend_map.items())),
            "backends": {
                name: cfg.public_record()
                for name, cfg in sorted(self.backends.items())
            },
        }

    def config_profile_name(self) -> str:
        gen = self.role_backend_map.get(ROLE_GENERATOR)
        cfg = self.backends.get(gen) if gen else None
        return cfg.config_profile_name() if cfg else "composite"

    def acceptable_identity_hashes(self) -> Sequence[str]:
        return [self.identity_hash]

    def build_runtime(
        self, *, log_dir: Optional[Path] = None
    ) -> RuntimeCoordinator:
        sub_backends: Dict[str, CompletionBackend] = {}
        local_coords: List[LocalLifecycleCoordinator] = []
        remote_coords: List[RemoteRuntimeCoordinator] = []
        for name, cfg in self.backends.items():
            runtime = cfg.build_runtime(log_dir=log_dir)
            if isinstance(runtime, LocalLifecycleCoordinator):
                local_coords.append(runtime)
                sub_backend = LocalRoutingBackend(runtime.router, cfg)
                # MONITOR-V2 (2.3): register the local routing backend on
                # the sub-coordinator so the composite coordinator's
                # set_usage_writer forwards local usage to its per-call sink.
                runtime.set_usage_backend(sub_backend)
                sub_backends[name] = sub_backend
            elif isinstance(runtime, RemoteRuntimeCoordinator):
                remote_coords.append(runtime)
                sub_backends[name] = runtime.backend
            else:
                raise TypeError(
                    f"CompositeBackendConfig: backend {name!r} produced an "
                    f"unsupported runtime {type(runtime).__name__}"
                )
        descriptor = self.build_descriptor()
        # The routing backend is attached to the coordinator (PR 4) so the
        # ``Backend*`` role adapters can serve Phase 1-2/Step 6 calls over
        # the same sub-backends the coordinator owns.
        composite_backend = CompositeCompletionBackend(sub_backends, descriptor)
        # Aggregating coordinator now keeps ALL sub-coordinators so second
        # backends' events/usage/summary are not lost.
        return CompositeRuntimeCoordinator(
            local_coords[0] if local_coords else None,
            remote_coords[0] if remote_coords else None,
            descriptor,
            backend=composite_backend,
            locals=local_coords,
            remotes=remote_coords,
        )


# ---------------------------------------------------------------------------
# V4.1 reasoning/backend compatibility policy
# ---------------------------------------------------------------------------


def _generator_backend_cfg(backend: Any) -> Any:
    """The sub-config that serves the Phase 2B generator role.

    ``None`` when a composite profile declares no generator binding at all
    (the run itself fails later with a missing model binding — that is not
    a reasoning-policy error).
    """
    if isinstance(backend, CompositeBackendConfig):
        gen_name = _composite_role_backend_name(backend, ROLE_GENERATOR)
        if gen_name is not None:
            return backend.backends.get(gen_name)
        # No explicit generator route and no sub-backend declaring the
        # generator role: fall back to a sub-backend with a ``default``
        # binding (the ``_model_ref_for`` default fallback) so the
        # reasoning policy keeps inspecting the model that would actually
        # serve generation.
        for sub in backend.backends.values():
            bindings = sub.build_descriptor().model_bindings or {}
            if "default" in bindings:
                return sub
        return None
    return backend


def _local_generator_server_args(backend: LocalLlamaBackendConfig) -> list:
    """The ``server_args`` list of the model that serves the generator role.

    For a local profile the generator role is bound to a model NAME (via
    ``_role_bindings``), while ``server_args`` is keyed by model KEY; this
    helper maps the generator binding back to its arg list so the reasoning
    policy inspects the exact args the generator model would launch with.
    """
    gen_name = backend._role_bindings().get(ROLE_GENERATOR)
    if gen_name is not None:
        for key, name in backend.model_names.items():
            if name == gen_name:
                return list(backend.server_args.get(key, []))
    # Fallback: first declared model's args (mirrors the local role binding
    # fallback when no explicit generator binding exists).
    for args in backend.server_args.values():
        return list(args)
    return []


def _reasoning_budget_from_server_args(args: Sequence[str]) -> Optional[int]:
    """The ``--reasoning-budget`` value in a server-args list, or ``None``.

    Returns ``None`` ONLY when the flag is absent (the profile cannot
    express a numeric reasoning budget). ``0`` means "no reasoning" (the
    B1 baseline on llama-server).

    A present-but-malformed flag fails closed with ``ValueError`` (A2 RV
    finding 3): a missing value (flag at the end of the list), a non-int
    value, or duplicate occurrences are profile-configuration errors and
    must never be conflated with an absent flag. ``validate_reasoning_backend``
    treats ``None`` as the accepted ``reasoning=0`` baseline, so silently
    mapping a malformed profile to ``None`` would let a broken config
    masquerade as the baseline.
    """
    occurrences = [i for i, arg in enumerate(args) if arg == "--reasoning-budget"]
    if not occurrences:
        return None
    if len(occurrences) > 1:
        raise ValueError(
            "--reasoning-budget appears more than once in server_args "
            f"({len(occurrences)} occurrences) — the profile is ambiguous; "
            "refusing to guess the intended budget."
        )
    index = occurrences[0]
    if index + 1 >= len(args):
        raise ValueError(
            "--reasoning-budget in server_args has no value — expected "
            "--reasoning-budget <int>."
        )
    raw = args[index + 1]
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"--reasoning-budget in server_args has a non-integer value "
            f"{raw!r} — expected --reasoning-budget <int>."
        ) from None


def validate_reasoning_backend(reasoning: Optional[int], backend: Any) -> None:
    if reasoning is None:
        reasoning = 0
    """Validate the Phase 2B reasoning budget against the generator backend.

    V4.1 A2 principle (owner-verified 2026-08-08: ``reasoning-budget 2048``
    works): local no longer blocks ``--reasoning > 0``. Reasoning for local
    ``llama-server`` transports is carried by the **server args**
    (``--reasoning-budget`` in the profile's ``server_args`` — see §3.4),
    NOT by ``CompletionRequest.request_options``; ``LocalOpenAIBackend``
    keeps rejecting any request_options as a library-level guard, so a
    local run must never send ``request_options`` and the CLI local path
    never emits them. The OpenCode backend maps 1/2/3 -> ``reasoningEffort``
    low/medium/high via request_options and remains the request-level
    transport.

    ``reasoning == 0`` (the B1 baseline) emits no request_options and is
    accepted on every backend. For a composite profile the check follows
    the ``generator`` role routing; the audit/repair/formatting adapters
    never carry request_options, so only the generator backend matters.

    A2 review fix (RV, commit 4ab250b): the local path is NOT a no-op — it
    fails closed unless the profile's generator server args can express the
    requested value, so CLI/config identity, server args and the actual
    transport always agree:

    * ``reasoning == 0`` with a local generator: the server args must NOT
      carry a nonzero ``--reasoning-budget`` (a profile that pins 2048
      would run reasoning the config identity denies — mismatch);
    * ``reasoning > 0`` with a local generator: the server args MUST carry
      a nonzero ``--reasoning-budget`` (the profile cannot express the
      requested reasoning without it — e.g. an arbitrary local profile
      with no reasoning server arg at all).

    A2 review fix (RV finding 3): a present-but-malformed
    ``--reasoning-budget`` (missing value, non-integer value, or duplicate
    occurrences) fails closed for EVERY reasoning level — ``None`` means
    "flag absent" (the accepted ``reasoning=0`` baseline), never "broken
    profile", so a malformed server-args profile can no longer masquerade
    as the baseline.

    Raises ``ValueError`` when the combination is unsupported / the profile
    cannot express the value; returns ``None`` otherwise.
    """
    gen = _generator_backend_cfg(backend)
    if gen is None or not isinstance(gen, LocalLlamaBackendConfig):
        # OpenCode / composite-remote: reasoning travels via request_options
        # (1/2/3 -> reasoningEffort low/medium/high); any value is expressible.
        return
    budget = _reasoning_budget_from_server_args(_local_generator_server_args(gen))
    if reasoning == 0:
        if budget not in (None, 0):
            raise ValueError(
                f"--reasoning 0 (B1 baseline) requested but the local generator "
                f"server_args pin --reasoning-budget {budget}: the server would "
                f"run reasoning the config identity denies. Set the profile's "
                f"--reasoning-budget to 0 (or remove it) for baseline runs."
            )
        return
    if budget is None or budget <= 0:
        raise ValueError(
            f"--reasoning {reasoning} requested but the local generator "
            f"server_args express no nonzero --reasoning-budget (plan §3.4: "
            f"2048): the profile cannot carry the requested reasoning. Add "
            f"--reasoning-budget 2048 to the generator model's server_args."
        )


# ---------------------------------------------------------------------------
# Role-adapter bridge (PR 4 / C3): build the Phase 1-2 + Step 6 callables
# over a built runtime's CompletionBackend.
# ---------------------------------------------------------------------------


def build_role_backend(
    cfg: BackendRuntimeConfig, runtime: RuntimeCoordinator
) -> CompletionBackend:
    """The ``CompletionBackend`` the ``Backend*`` role adapters use.

    ``run_chapter_strict`` is backend-agnostic, but its five injected
    callables are the ``Backend*`` transport adapters over a single
    ``CompletionBackend``. Each tagged config resolves to the backend the
    runtime actually serves:

    * ``local_llama`` -> ``LocalRoutingBackend`` over the router (ensures
      the right model is resident before delegating);
    * ``opencode_server`` -> the coordinator's remote backend (already
      carrying the managed server's ephemeral credentials when managed);
    * ``composite`` -> the ``CompositeCompletionBackend`` attached to the
      coordinator by ``build_runtime`` (routes by model_ref).

    No new process/HTTP client is created here; it only wires the already
    built runtime.
    """
    if isinstance(cfg, LocalLlamaBackendConfig):
        backend = LocalRoutingBackend(runtime.router, cfg)
        # MONITOR-V2 (2.3): register the local routing backend on the
        # coordinator so the strict runner's runtime.set_usage_writer()
        # reaches the per-call sink of the LocalOpenAIBackend instances.
        attach = getattr(runtime, "set_usage_backend", None)
        if attach is not None:
            attach(backend)
        return backend
    if isinstance(cfg, OpenCodeBackendConfig):
        return runtime.backend
    if isinstance(cfg, CompositeBackendConfig):
        return runtime.backend
    raise TypeError(
        f"build_role_backend: unsupported config {type(cfg).__name__}"
    )


def build_role_adapters(
    cfg: BackendRuntimeConfig,
    runtime: RuntimeCoordinator,
    *,
    json_retry_policy: Optional[JsonRetryPolicy] = None,
    bible_text: str = "",
) -> Tuple[Any, Any, Any, Any, Any]:
    """The five role adapters ``run_chapter_strict`` needs injected.

    Return order matches ``build_strict_lifecycle``: ``(model_caller,
    qwen_evaluator, gemma_selector, qwen_audit_evaluator,
    gemma_audit_evaluator)``. Imported lazily so ``runtime_config`` stays
    importable without ``backend_role_adapters`` (no import cycle).

    ``json_retry_policy`` (B4/B10, optional) overrides the JSON-resilience
    retry policy of every role adapter — generation, Qwen fidelity gate,
    Gemma selector, Qwen audit, Gemma audit (default ``JsonRetryPolicy()``,
    ``max_retries=2``). A runtime-config override is wired by the caller (the
    CLI); the policy is a resilience parameter and does not change backend
    identity.

    ``bible_text`` (B7) is the rendered book-memory section appended to every
    Phase 2C/3B prompt (Qwen fidelity, Step 6 Qwen/Gemma audit) so the model
    sees narrator gender/characters/facts when judging fidelity or reviewing
    Russian. The Phase 2B generation prompt reads ``bible_text`` from
    ``PromptBundle`` directly; this parameter is only used by the fidelity
    and audit adapters.
    """
    from pact_v4.runtime.backend_role_adapters import (
        BackendGemmaAuditEvaluator,
        BackendGemmaAuditEvaluatorConfig,
        BackendGemmaSelector,
        BackendGemmaSelectorConfig,
        BackendModelCaller,
        BackendModelCallerConfig,
        BackendQwenAuditEvaluator,
        BackendQwenAuditEvaluatorConfig,
        BackendQwenEvaluator,
        BackendQwenEvaluatorConfig,
    )

    retry = json_retry_policy or JsonRetryPolicy()
    backend = build_role_backend(cfg, runtime)
    return (
        BackendModelCaller(backend, config=BackendModelCallerConfig(retry=retry)),
        BackendQwenEvaluator(
            backend, config=BackendQwenEvaluatorConfig(retry=retry, bible_text=bible_text),
        ),
        BackendGemmaSelector(
            backend, config=BackendGemmaSelectorConfig(retry=retry),
        ),
        BackendQwenAuditEvaluator(
            backend,
            config=BackendQwenAuditEvaluatorConfig(retry=retry, bible_text=bible_text),
        ),
        BackendGemmaAuditEvaluator(
            backend,
            config=BackendGemmaAuditEvaluatorConfig(retry=retry, bible_text=bible_text),
        ),
    )


def build_repair_adapters(
    cfg: BackendRuntimeConfig,
    runtime: RuntimeCoordinator,
    *,
    json_retry_policy: Optional[JsonRetryPolicy] = None,
    bible_text: str = "",
) -> Tuple[Any, Any, Any, Any]:
    """The Phase 4 repair callables ``run_chapter_strict`` needs injected.

    Return order: ``(repair_caller, region_fidelity_gate, qwen_audit_evaluator,
    gemma_audit_evaluator)``. ``region_fidelity_gate`` is the L2b narrow
    per-region re-gate (``BackendRegionFidelityGate``); the full-chunk Qwen
    re-gate is no longer used by repair — unedited PIDs are covered by the
    convergence re-audit. All are ``Backend*`` adapters over the coordinator
    ``CompletionBackend`` (``build_role_backend``), never local lifecycle
    adapters — Phase 4 repair must run through the same backend-neutral
    boundary in local, remote and composite profiles (dual-mode rule; no
    retrofit needed). Imported lazily to avoid an import cycle with
    ``backend_role_adapters``.

    ``json_retry_policy`` (B4/B10, optional) overrides the JSON-resilience
    retry policy of every Phase 4 adapter — repair caller, L2b region
    re-gate, Step 6 Qwen audit re-used during convergence, Gemma audit
    (default ``JsonRetryPolicy()``, ``max_retries=2``). A runtime-config
    override is wired by the caller (the CLI); the policy is a resilience
    parameter and does not change backend identity.
    """
    from pact_v4.runtime.backend_role_adapters import (
        BackendGemmaAuditEvaluator,
        BackendGemmaAuditEvaluatorConfig,
        BackendQwenAuditEvaluator,
        BackendQwenAuditEvaluatorConfig,
        BackendRegionFidelityGate,
        BackendRegionFidelityGateConfig,
        BackendRepairCaller,
        BackendRepairCallerConfig,
    )

    retry = json_retry_policy or JsonRetryPolicy()
    backend = build_role_backend(cfg, runtime)
    return (
        BackendRepairCaller(backend, config=BackendRepairCallerConfig(retry=retry)),
        BackendRegionFidelityGate(
            backend, config=BackendRegionFidelityGateConfig(retry=retry),
        ),
        BackendQwenAuditEvaluator(
            backend, config=BackendQwenAuditEvaluatorConfig(
                retry=retry, bible_text=bible_text,
            ),
        ),
        BackendGemmaAuditEvaluator(
            backend, config=BackendGemmaAuditEvaluatorConfig(
                retry=retry, bible_text=bible_text,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Providers registry (PROVIDERS-REGISTRY card): providers.yaml model catalog
# + --translator/--reviewer role mapping (owner decision 2026-08-14).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderModel:
    """One model entry in the registry: full ``provider/model`` ref + contract.

    ``reasoning_variants`` are the ``reasoningEffort`` values the model
    declares in the opencode provider catalog (verified against
    ``opencode models <provider> --verbose`` on serve 1.18.18, 2026-08-14).
    The serve relay does NOT validate ``reasoningEffort`` (any string is
    accepted and proxied), so this contract is the authoritative mapping
    guide for ``--reasoning N`` (1/2/3 -> nearest declared variant).
    """

    ref: str
    reasoning_variants: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str) or "/" not in self.ref:
            raise ValueError(
                f"ProviderModel: ref {self.ref!r} must be 'provider/model'"
            )
        if not isinstance(self.reasoning_variants, tuple) or not self.reasoning_variants:
            raise ValueError(
                "ProviderModel: reasoning_variants must be a non-empty list of "
                f"reasoningEffort values (model {self.ref!r})"
            )


# Canonical reasoningEffort ladder (declared order across the opencode
# catalog). Used to map --reasoning 1/2/3 to the nearest DECLARED variant
# when the model's contract does not list the canonical value itself
# (e.g. deepseek-v4-flash declares {low, high, max}: 2 -> high).
REASONING_EFFORT_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Canonical --reasoning level -> reasoningEffort value (the B1/opencode
# transport default when the model declares no contract).
DEFAULT_REASONING_EFFORT = {1: "low", 2: "medium", 3: "high"}


@dataclass(frozen=True)
class ProvidersRegistry:
    """Loaded ``providers.yaml``: provider id -> alias -> model."""

    providers: Mapping[str, Mapping[str, ProviderModel]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", dict(self.providers))
        norm_providers: Dict[str, str] = {}
        for provider_id in self.providers:
            lower = provider_id.lower()
            if lower in norm_providers:
                raise ValueError(
                    f"ProvidersRegistry: duplicate provider id case-insensitively {provider_id!r} "
                    f"collides with {norm_providers[lower]!r}"
                )
            norm_providers[lower] = provider_id
        object.__setattr__(self, "_norm_provider_map", norm_providers)  # type: ignore[attr-defined]
        alias_lower_to_ref: Dict[str, Tuple[str, str]] = {}
        for provider_id, models in self.providers.items():
            for alias in models:
                lower = alias.lower()
                if lower in alias_lower_to_ref:
                    prev_provider, prev_alias = alias_lower_to_ref[lower]
                    raise ValueError(
                        f"ProvidersRegistry: duplicate alias {alias!r} (normalized {lower!r}) "
                        f"across providers {prev_provider!r}/{prev_alias!r} and {provider_id!r}/{alias!r} — "
                        f"aliases must be globally unique case-insensitively; use provider-qualified resolution only after fixing the registry"
                    )
                alias_lower_to_ref[lower] = (provider_id, alias)
        object.__setattr__(self, "_alias_index", alias_lower_to_ref)  # type: ignore[attr-defined]
        bare_map: Dict[str, ProviderModel] = {}
        for (provider_id, alias), model in self._iter_alias_models():  # type: ignore[attr-defined]
            bare_map[alias.lower()] = model
        object.__setattr__(self, "_bare_index", bare_map)  # type: ignore[attr-defined]

    def _iter_alias_models(self):  # type: ignore[no-untyped-def]
        for provider_id, models in self.providers.items():
            for alias, model in models.items():
                yield (provider_id, alias), model

    def resolve_bare(self, alias: str) -> ProviderModel:
        """Resolve a bare alias case-insensitively via the global index."""
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"providers registry: bare alias must be a non-empty string, got {alias!r}")
        lower = alias.lower()
        bare_index: Dict[str, ProviderModel] = getattr(self, "_bare_index", {})  # type: ignore[attr-defined]
        model = bare_index.get(lower)
        if model is None:
            raise ValueError(
                f"providers registry: unknown bare alias {alias!r} "
                f"(known aliases: {sorted(bare_index)})"
            )
        return model

    def resolve(self, spec: str) -> ProviderModel:
        """Resolve ``<provider>/<alias>`` (CLI flag value) to a model entry.

        The flag format uses a SLASH (not a dash) because model ids already
        contain dashes (``deepseek-v4-flash``) — a dash separator would be
        ambiguous (owner decision 2026-08-14). Lookup is case-insensitive for
        both provider and alias; duplicates are rejected at load time.
        """
        if not isinstance(spec, str):
            raise ValueError(f"providers registry: spec must be a string, got {spec!r}")
        parts = spec.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"providers registry: {spec!r} is not '<provider>/<alias>' "
                "(slash separator; e.g. opencode-go/deepseek4flash)"
            )
        provider, alias = parts
        norm_map: Dict[str, str] = getattr(self, "_norm_provider_map", {})  # type: ignore[attr-defined]
        canonical_provider = norm_map.get(provider.lower())
        if canonical_provider is None:
            raise ValueError(
                f"providers registry: unknown provider {provider!r} "
                f"(known: {sorted(self.providers)})"
            )
        models = self.providers.get(canonical_provider)
        assert models is not None
        lower_alias = alias.lower()
        for key, model in models.items():
            if key.lower() == lower_alias:
                return model
        raise ValueError(
            f"providers registry: unknown alias {canonical_provider!r}/{alias!r} "
            f"(known: {sorted(models)})"
        )


def load_providers_registry(path: Path) -> ProvidersRegistry:
    """Load ``providers.yaml`` into a ``ProvidersRegistry``.

    Fail-closed on malformed entries (unknown kind, missing/invalid model
    ref, missing reasoning variants) so a typo in the registry can never
    silently resolve to the wrong model.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(
            f"{path}: providers registry file not found (expected a "
            "providers.yaml with 'providers:' / kind / models / "
            "reasoning_contract.variants)"
        )
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ValueError(
            f"{path}: providers.yaml requires PyYAML (pip install pyyaml)"
        ) from exc
    payload = yaml.safe_load(raw)
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("providers"), Mapping
    ):
        raise ValueError(
            f"{path}: providers.yaml must contain a 'providers:' mapping"
        )
    providers: Dict[str, Dict[str, ProviderModel]] = {}
    for provider_id, provider_entry in payload["providers"].items():
        if not isinstance(provider_entry, Mapping):
            raise ValueError(
                f"{path}: provider {provider_id!r} must be a mapping "
                f"(kind + models), got {type(provider_entry).__name__}"
            )
        kind = provider_entry.get("kind")
        if kind != "opencode_server":
            raise ValueError(
                f"{path}: provider {provider_id!r} has unsupported kind {kind!r} "
                "(only 'opencode_server' is supported)"
            )
        raw_models = provider_entry.get("models")
        if not isinstance(raw_models, Mapping) or not raw_models:
            raise ValueError(
                f"{path}: provider {provider_id!r} must declare a non-empty "
                "'models:' mapping"
            )
        models: Dict[str, ProviderModel] = {}
        for alias, raw_model in raw_models.items():
            if not isinstance(raw_model, Mapping):
                raise ValueError(
                    f"{path}: model {provider_id!r}/{alias!r} must be a mapping "
                    "with 'ref' and 'reasoning_contract.variants'"
                )
            ref = raw_model.get("ref")
            if not isinstance(ref, str) or "/" not in ref:
                raise ValueError(
                    f"{path}: model {provider_id!r}/{alias!r} has invalid ref "
                    f"{ref!r} (must be 'provider/model')"
                )
            contract = raw_model.get("reasoning_contract") or {}
            variants = contract.get("variants") if isinstance(contract, Mapping) else None
            if not isinstance(variants, list) or not variants:
                raise ValueError(
                    f"{path}: model {provider_id!r}/{alias!r} must declare "
                    "reasoning_contract.variants (the reasoningEffort values "
                    "the model supports, checked against the opencode provider "
                    "catalog — see configs/providers.yaml)"
                )
            bad = [v for v in variants if v not in REASONING_EFFORT_LADDER]
            if bad:
                raise ValueError(
                    f"{path}: model {provider_id!r}/{alias!r} has unknown "
                    f"reasoning variant(s) {bad} (ladder: {REASONING_EFFORT_LADDER})"
                )
            models[alias] = ProviderModel(
                ref=ref,
                reasoning_variants=tuple(str(v) for v in variants),
            )
        providers[provider_id] = models
    return ProvidersRegistry(providers=providers)


def resolve_role_model(registry: ProvidersRegistry, spec: str) -> ProviderModel:
    """Resolve a CLI role flag value (``<provider>/<alias>``) via the registry."""
    return registry.resolve(spec)


def nearest_declared_effort(
    level: int, variants: Sequence[str]
) -> str:
    """Map ``--reasoning N`` (1/2/3) to the model's nearest DECLARED variant.

    When the canonical value for the level (1=low, 2=medium, 3=high) is not
    declared by the model contract, the nearest declared value on the
    canonical ladder is used (e.g. deepseek-v4-flash {low, high, max}:
    2 -> high). Ties resolve toward the HIGHER declared value (a stronger
    reasoning level is closer to the user's intent than a weaker one).
    """
    target = DEFAULT_REASONING_EFFORT[level]
    if target in variants:
        return target
    target_index = REASONING_EFFORT_LADDER.index(target)
    best: Optional[str] = None
    best_distance = 10**9
    for variant in variants:
        index = REASONING_EFFORT_LADDER.index(variant)
        distance = abs(index - target_index)
        if distance < best_distance or (
            distance == best_distance
            and index > REASONING_EFFORT_LADDER.index(best or "")
        ):
            best = variant
            best_distance = distance
    assert best is not None
    return best


def build_reasoning_effort_map(
    model: ProviderModel,
) -> Dict[int, str]:
    """The ``--reasoning`` level -> ``reasoningEffort`` map for a model.

    Returns ``DEFAULT_REASONING_EFFORT`` when every canonical value is
    declared (the common case for openai models); otherwise the nearest
    declared value per level (the reasoning contract, owner decision
    2026-08-14: use the nearest declared variant rather than sending an
    undeclared string).
    """
    variants = tuple(model.reasoning_variants)
    if all(DEFAULT_REASONING_EFFORT[level] in variants for level in (1, 2, 3)):
        return dict(DEFAULT_REASONING_EFFORT)
    return {level: nearest_declared_effort(level, variants) for level in (1, 2, 3)}


# Roles the runtime resolves through a documented fallback when a composite
# profile omits them: ``repair`` rides the generator binding
# (``backend_role_adapters`` resolves ("repair", "generator");
# ``selective_repair.repair_model_ref`` uses ("generator", "default")) and
# ``entity_extractor`` is derived from the audit (Qwen) model at runtime
# (B3 ``_EntityRoleView``). The provider override follows the same chain so
# ``--translator``/``--reviewer`` do not turn a valid composite into a
# "role is not routed" error.
_COMPOSITE_ROLE_FALLBACKS = {
    ROLE_REPAIR: ROLE_GENERATOR,
    ROLE_ENTITY_EXTRACTOR: ROLE_QWEN_AUDIT,
}


def _resolve_role_backend(
    role_backend_map: Mapping[str, str],
    bindings_by_backend: Mapping[str, Mapping[str, str]],
    role: str,
) -> Optional[str]:
    """The sub-backend serving ``role`` (or ``None`` when unrouted).

    Resolution order mirrors the runtime: an explicit ``role_backend_map``
    entry wins; else the first sub-backend whose descriptor declares the
    role; else the documented role fallback (``repair`` -> ``generator``,
    ``entity_extractor`` -> ``qwen_audit``) resolved the same way (explicit
    route first, then a sub-backend declaring the fallback role or
    ``default``). This is the single source of truth shared by
    ``CompositeBackendConfig.build_descriptor`` (which model ref the role
    adapters resolve), ``CompositeCompletionBackend`` (which concrete
    backend serves that ref) and ``apply_role_models`` (where a
    ``--translator``/``--reviewer`` override lands), so the three can never
    disagree about which backend serves a role.
    """
    backend_name = role_backend_map.get(role)
    if backend_name is not None:
        return backend_name
    for name, bindings in bindings_by_backend.items():
        if role in bindings:
            return name
    fallback_role = _COMPOSITE_ROLE_FALLBACKS.get(role)
    if fallback_role is None:
        return None
    backend_name = role_backend_map.get(fallback_role)
    if backend_name is not None:
        return backend_name
    for name, bindings in bindings_by_backend.items():
        if fallback_role in bindings or "default" in bindings:
            return name
    return None


def _composite_ref_ambiguity(
    role_backend_map: Mapping[str, str],
    refs_by_backend: Mapping[str, Mapping[str, str]],
) -> Dict[str, Tuple[str, ...]]:
    """Model refs whose selected roles resolve to different concrete backends.

    Returns ``{ref: (backend, ...)}`` for every ref claimed by roles that
    resolve to MORE THAN ONE backend. A composite routes by ``model_ref``
    alone (requests carry no role), so such a ref can never be served
    coherently: whichever single backend the ref maps to, at least one
    role's requests would silently reach the wrong backend. Callers fail
    closed instead of picking a sorted-role winner (RV t_edb1033a HIGH).
    """
    ref_to_backends: Dict[str, set] = {}
    for role in sorted({r for mb in refs_by_backend.values() for r in mb}):
        backend_name = _resolve_role_backend(role_backend_map, refs_by_backend, role)
        if backend_name is None or backend_name not in refs_by_backend:
            continue
        ref = refs_by_backend[backend_name].get(role)
        if ref:
            ref_to_backends.setdefault(ref, set()).add(backend_name)
    return {
        ref: tuple(sorted(backends))
        for ref, backends in ref_to_backends.items()
        if len(backends) > 1
    }


def _composite_ambiguity_message(ambiguity: Mapping[str, Tuple[str, ...]]) -> str:
    return (
        "model_ref(s) claimed by roles on different concrete backends: "
        + "; ".join(
            f"{ref} ({', '.join(backends)})"
            for ref, backends in sorted(ambiguity.items())
        )
        + " — a composite routes by model_ref alone and cannot serve one ref "
        "from two backends; bind the colliding roles to the same backend or "
        "give them distinct model refs"
    )


def _composite_role_backend_name(
    cfg: CompositeBackendConfig, role: str
) -> Optional[str]:
    """The sub-backend serving ``role`` in a composite profile (or ``None``).

    Thin wrapper over ``_resolve_role_backend``; see it for the resolution
    order.
    """
    refs_by_backend = {
        name: (sub.build_descriptor().model_bindings or {})
        for name, sub in cfg.backends.items()
    }
    return _resolve_role_backend(cfg.role_backend_map, refs_by_backend, role)


def apply_role_models(
    cfg: Any,
    role_models: Mapping[str, str],
) -> Any:
    """Override the model refs of the named roles in a backend config.

    Used by ``--translator``/``--reviewer``: the loaded runtime config's
    OpenCode backend(s) get their ``model_bindings`` updated for the mapped
    roles. A composite profile routes each role to its backend via
    ``role_backend_map`` (falling back to the first sub-backend declaring
    the role, then to the documented runtime fallback role — ``repair`` ->
    ``generator``, ``entity_extractor`` -> ``qwen_audit``), so the override
    is applied per-role to the routing backend; a role that routes to a
    LOCAL backend fails loudly — local models are bound by name and cannot
    serve a remote provider ref, and silently dropping the override would
    make the flag a no-op.
    """
    if not role_models:
        return cfg
    if isinstance(cfg, OpenCodeBackendConfig):
        server = replace(
            cfg.server,
            model_bindings={**dict(cfg.server.model_bindings), **dict(role_models)},
        )
        return replace(cfg, server=server)
    if isinstance(cfg, CompositeBackendConfig):
        new_backends = dict(cfg.backends)
        for role, ref in role_models.items():
            backend_name = _composite_role_backend_name(cfg, role)
            if backend_name is None:
                raise ValueError(
                    f"apply_role_models: role {role!r} is not routed by the "
                    "composite profile (role_backend_map has no entry, no "
                    "sub-backend declares it, and no documented runtime "
                    "fallback role resolves it)"
                )
            sub = new_backends[backend_name]
            if isinstance(sub, LocalLlamaBackendConfig):
                raise ValueError(
                    f"apply_role_models: role {role!r} routes to local backend "
                    f"{backend_name!r}; --translator/--reviewer cannot override "
                    "a local model (bind the role to an opencode_server backend)"
                )
            new_backends[backend_name] = apply_role_models(sub, {role: ref})
        return replace(cfg, backends=new_backends)
    if isinstance(cfg, LocalLlamaBackendConfig):
        raise ValueError(
            "role model overrides (--translator/--reviewer) cannot be applied to "
            "a local_llama backend; use an opencode_server or composite profile"
        )
    raise ValueError(
        f"apply_role_models: unsupported config kind {type(cfg).__name__}"
    )


def _generator_backend_name(cfg: CompositeBackendConfig) -> Optional[str]:
    """The sub-backend that serves the generator role (or ``None``)."""
    name = cfg.role_backend_map.get(ROLE_GENERATOR)
    if name is not None:
        return name
    for name, sub in cfg.backends.items():
        bindings = sub.build_descriptor().model_bindings or {}
        if ROLE_GENERATOR in bindings or "default" in bindings:
            return name
    return None


def _set_generator_reasoning_effort_map(cfg: Any, effort_map: Mapping[int, str]) -> Any:
    """Set the reasoning-effort map on the backend serving the generator role.

    Generation is the only phase that carries a ``reasoning`` request
    option, so the contract-derived effort map belongs on the generator's
    backend. A local generator keeps ``None`` (its reasoning travels via
    server args, never request_options).
    """
    if isinstance(cfg, OpenCodeBackendConfig):
        return replace(
            cfg,
            server=replace(cfg.server, reasoning_effort_map=dict(effort_map)),
        )
    if isinstance(cfg, CompositeBackendConfig):
        name = _generator_backend_name(cfg)
        if name is None:
            return cfg
        sub = cfg.backends[name]
        if isinstance(sub, LocalLlamaBackendConfig):
            return cfg
        new_backends = dict(cfg.backends)
        new_backends[name] = _set_generator_reasoning_effort_map(sub, effort_map)
        return replace(cfg, backends=new_backends)
    return cfg


def apply_provider_flags(
    cfg: Any,
    registry: ProvidersRegistry,
    *,
    translator: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> Any:
    """Apply ``--translator``/``--reviewer`` role model overrides to a config.

    ``translator``/``reviewer`` are registry specs (``<provider>/<alias>``).
    Returns a NEW config (the input is untouched):

    * ``--translator`` binds the resolved model to generator + repair;
    * ``--reviewer`` binds it to every audit role (qwen_audit,
      fidelity_reviewer, russian_selector, entity_extractor);
    * when a translator is given, the reasoning-effort map is derived from
      the translator model's reasoning contract (nearest declared variant)
      and set on the backend serving the generator role — so
      ``--reasoning 2`` on deepseek-v4-flash ({low, high, max}) sends
      ``high``, the contract-correct value.

    Defaults are NOT changed when the flags are absent: the caller only
    invokes this when at least one flag is given, and the resolved
    model/provider refs enter the backend descriptor (identity) — a flag
    change invalidates cache/resume.
    """
    role_models: Dict[str, str] = {}
    if translator:
        model = registry.resolve(translator)
        for role in TRANSLATOR_ROLES:
            role_models[role] = model.ref
    if reviewer:
        model = registry.resolve(reviewer)
        for role in REVIEWER_ROLES:
            role_models[role] = model.ref
    if not role_models:
        return cfg
    cfg = apply_role_models(cfg, role_models)
    if translator:
        model = registry.resolve(translator)
        cfg = _set_generator_reasoning_effort_map(
            cfg, build_reasoning_effort_map(model)
        )
    return cfg


# ---------------------------------------------------------------------------
# Loader (dict -> tagged config). Values of secrets are never read here:
# only env-var *names* are recorded (plan §12).
# ---------------------------------------------------------------------------


def load_runtime_config(payload: Mapping[str, Any]) -> BackendRuntimeConfig:
    """Load a tagged runtime config from a JSON/YAML-style mapping.

    Supported ``kind`` values: ``local_llama``, ``opencode_server``,
    ``composite``. ``opencode_server`` accepts ``server_mode``
    ``external``/``managed`` and records auth via env var names only.
    """
    kind = payload.get("kind")
    if kind == KIND_LOCAL_LLAMA:
        return _load_local(payload)
    if kind == "opencode_server":
        return _load_opencode(payload)
    if kind == KIND_COMPOSITE:
        return _load_composite(payload)
    raise ValueError(f"load_runtime_config: unknown kind {kind!r}")


SUPPORTED_LOCAL_MODEL_KEYS = frozenset({"gemma", "qwen"})
_ALLOWED_LOCAL_KEYS = frozenset({
    "kind", "exe", "device", "host", "port",
    "startup_timeout", "unload_timeout",
    "model_paths", "model_names", "server_args",
})


def _load_local(payload: Mapping[str, Any]) -> LocalLlamaBackendConfig:
    unknown = set(payload) - _ALLOWED_LOCAL_KEYS
    if unknown:
        raise ValueError(
            "load_runtime_config[local_llama]: unsupported key(s) "
            f"{sorted(unknown)} — allowed: {sorted(_ALLOWED_LOCAL_KEYS)}"
        )
    exe_raw = payload.get("exe")
    if not isinstance(exe_raw, str) or not exe_raw.strip():
        raise ValueError(
            "load_runtime_config[local_llama]: exe is required and must be a non-empty string"
        )
    device = payload.get("device", "SYCL0")
    if not isinstance(device, str) or not device.strip():
        raise ValueError("load_runtime_config[local_llama]: device must be a non-empty string")
    host = payload.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("load_runtime_config[local_llama]: host must be a non-empty string")
    # Host locality: only loopback hosts are permitted for local profile.
    # Reject external, malformed, or non-local hosts before lifecycle.
    host_stripped = host.strip()
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if host_stripped not in allowed_hosts:
        # Also allow 127.x.x.x loopback range via ipaddress check
        import ipaddress as _ipaddr
        try:
            ip = _ipaddr.ip_address(host_stripped)
            if not ip.is_loopback:
                raise ValueError
        except ValueError:
            raise ValueError(
                "load_runtime_config[local_llama]: host must be a local loopback host "
                f"(allowed: {sorted(allowed_hosts)} or 127.* loopback), got {host!r}"
            ) from None
    port_raw = payload.get("port", 8093)
    if isinstance(port_raw, bool):
        raise ValueError(f"load_runtime_config[local_llama]: port must be an integer 1-65535, got {port_raw!r} (bool is not a valid port)")
    if not isinstance(port_raw, int):
        raise ValueError(f"load_runtime_config[local_llama]: port must be an integer 1-65535, got {port_raw!r}")
    port = port_raw
    if not 1 <= port <= 65535:
        raise ValueError(f"load_runtime_config[local_llama]: port must be 1-65535, got {port}")
    for timeout_key in ("startup_timeout", "unload_timeout"):
        if timeout_key in payload:
            try:
                val = float(payload[timeout_key])
            except (TypeError, ValueError):
                raise ValueError(f"load_runtime_config[local_llama]: {timeout_key} must be a number, got {payload[timeout_key]!r}") from None
            if timeout_key == "startup_timeout" and val <= 0:
                raise ValueError(f"load_runtime_config[local_llama]: startup_timeout must be > 0, got {val}")
            if timeout_key == "unload_timeout" and val < 0:
                raise ValueError(f"load_runtime_config[local_llama]: unload_timeout must be >= 0, got {val}")
    raw_model_paths = payload.get("model_paths")
    if not isinstance(raw_model_paths, Mapping) or not raw_model_paths:
        raise ValueError("load_runtime_config[local_llama]: model_paths is required and must be a non-empty mapping")
    raw_model_names = payload.get("model_names")
    if not isinstance(raw_model_names, Mapping) or not raw_model_names:
        raise ValueError("load_runtime_config[local_llama]: model_names is required and must be a non-empty mapping")
    raw_server_args = payload.get("server_args")
    if not isinstance(raw_server_args, Mapping) or not raw_server_args:
        raise ValueError("load_runtime_config[local_llama]: server_args is required and must be a non-empty mapping")
    for key in raw_model_paths:
        if key not in SUPPORTED_LOCAL_MODEL_KEYS:
            raise ValueError(f"load_runtime_config[local_llama]: unsupported model key {key!r} in model_paths (allowed: {sorted(SUPPORTED_LOCAL_MODEL_KEYS)})")
    for key in raw_model_names:
        if key not in SUPPORTED_LOCAL_MODEL_KEYS:
            raise ValueError(f"load_runtime_config[local_llama]: unsupported model key {key!r} in model_names (allowed: {sorted(SUPPORTED_LOCAL_MODEL_KEYS)})")
    for key in raw_server_args:
        if key not in SUPPORTED_LOCAL_MODEL_KEYS:
            raise ValueError(f"load_runtime_config[local_llama]: unsupported model key {key!r} in server_args (allowed: {sorted(SUPPORTED_LOCAL_MODEL_KEYS)})")
    keys_paths = set(raw_model_paths)
    keys_names = set(raw_model_names)
    keys_args = set(raw_server_args)
    if not (keys_paths == keys_names == keys_args):
        raise ValueError(
            "load_runtime_config[local_llama]: model_paths, model_names and server_args must describe the same "
            f"non-empty model keys; got model_paths={sorted(keys_paths)}, model_names={sorted(keys_names)}, server_args={sorted(keys_args)}"
        )
    model_paths: Dict[str, Path] = {}
    for k, v in raw_model_paths.items():
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"load_runtime_config[local_llama]: model_paths[{k!r}] must be a non-empty string path, got {v!r}")
        model_paths[k] = Path(v)
    model_names: Dict[str, str] = {}
    for k, v in raw_model_names.items():
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"load_runtime_config[local_llama]: model_names[{k!r}] must be a non-empty string, got {v!r}")
        model_names[k] = v
    server_args: Dict[str, List[str]] = {}
    for k, v in raw_server_args.items():
        if not isinstance(v, list):
            raise ValueError(f"load_runtime_config[local_llama]: server_args[{k!r}] must be a list of strings, got {type(v).__name__}")
        for item in v:
            if not isinstance(item, str):
                raise ValueError(
                    "load_runtime_config[local_llama]: server_args must contain only strings, got "
                    f"{k}: {v!r} (quote YAML 1.1 bool words like `on`/`off`/`yes`/`no` in the runtime config)"
                )
        server_args[k] = list(v)
    return LocalLlamaBackendConfig(
        exe=Path(exe_raw),
        device=device,
        host=host,
        model_paths=model_paths,
        model_names=model_names,
        server_args=server_args,
        port=port,
        startup_timeout=float(payload.get("startup_timeout", 240.0)),
        unload_timeout=float(payload.get("unload_timeout", 30.0)),
    )


def _load_opencode(payload: Mapping[str, Any]) -> OpenCodeBackendConfig:
    _auth_raw = payload.get("auth")
    if _auth_raw is None:
        auth: Mapping[str, Any] = {}
    elif not isinstance(_auth_raw, Mapping):
        raise ValueError(
            "load_runtime_config[opencode_server]: auth must be a "
            f"mapping, got {type(_auth_raw).__name__}: {_auth_raw!r} "
            "(expected a block with type, username_env, password_env; omit the key or use null for defaults)"
        )
    else:
        auth = _auth_raw
    # Nested auth validation: fail-closed on unsupported auth keys (e.g. type must be declared)
    if isinstance(auth, Mapping) and auth:
        _allowed_auth_keys = {"type", "username_env", "password_env"}
        _unknown_auth = set(auth) - _allowed_auth_keys
        if _unknown_auth:
            raise ValueError(
                "load_runtime_config[opencode_server]: unsupported auth key(s) "
                f"{sorted(_unknown_auth)} — allowed: {sorted(_allowed_auth_keys)}"
            )
        if "type" in auth:
            auth_type = auth.get("type")
            if auth_type not in ("basic_env", None):
                raise ValueError(
                    "load_runtime_config[opencode_server]: unsupported auth.type "
                    f"{auth_type!r} — expected 'basic_env'"
                )
    username_env = auth.get("username_env") if isinstance(auth, Mapping) else None
    password_env = auth.get("password_env") if isinstance(auth, Mapping) else None
    managed = payload.get("managed")
    managed_spec: Optional[ManagedServerSpec] = None
    if managed is not None and not isinstance(managed, Mapping):
        raise ValueError(
            "load_runtime_config[opencode_server]: managed must be a "
            f"mapping, got {type(managed).__name__}: {managed!r} "
            "(expected a block with hostname, port, pinned_server_version, server_version_policy; omit the key or use null for defaults)"
        )
    if isinstance(managed, Mapping):
        _allowed_managed_keys = {"hostname", "port", "pinned_server_version", "server_version_policy"}
        _unknown_managed = set(managed) - _allowed_managed_keys
        if _unknown_managed:
            raise ValueError(
                "load_runtime_config[opencode_server]: unsupported managed key(s) "
                f"{sorted(_unknown_managed)} — allowed: {sorted(_allowed_managed_keys)}"
            )
        managed_spec = ManagedServerSpec(
            hostname=managed.get("hostname", DEFAULT_HOSTNAME),
            port=int(managed.get("port", 4096)),
            pinned_server_version=managed.get(
                "pinned_server_version",
                payload.get("pinned_server_version", OPENCODE_PINNED_SERVER_VERSION),
            ),
            server_version_policy=managed.get(
                "server_version_policy",
                payload.get("server_version_policy", "compatible_minor"),
            ),
        )
    remote_budget_payload = payload.get("remote_budget")
    remote_budget: Optional[RemoteBudget] = None
    if isinstance(remote_budget_payload, Mapping):
        _allowed_budget_keys = {"max_requests_per_chapter", "max_retry_requests_per_chapter", "max_wait_seconds_on_rate_limit", "max_reported_cost"}
        _unknown_budget = set(remote_budget_payload) - _allowed_budget_keys
        if _unknown_budget:
            raise ValueError(
                "load_runtime_config[opencode_server]: unsupported remote_budget key(s) "
                f"{sorted(_unknown_budget)} — allowed: {sorted(_allowed_budget_keys)}"
            )
        remote_budget = RemoteBudget(
            max_requests_per_chapter=int(
                remote_budget_payload.get(
                    "max_requests_per_chapter",
                    RemoteBudget().max_requests_per_chapter,
                )
            ),
            max_retry_requests_per_chapter=int(
                remote_budget_payload.get(
                    "max_retry_requests_per_chapter",
                    RemoteBudget().max_retry_requests_per_chapter,
                )
            ),
            max_wait_seconds_on_rate_limit=float(
                remote_budget_payload.get(
                    "max_wait_seconds_on_rate_limit",
                    RemoteBudget().max_wait_seconds_on_rate_limit,
                )
            ),
            max_reported_cost=remote_budget_payload.get("max_reported_cost"),
        )
    elif remote_budget_payload is not None:
        # An explicit non-mapping value (scalar/list/bool) is a malformed
        # budget block, not an absent one: silently falling back to the 500
        # default would start/resume the run with a different limit and a
        # different identity than the operator asked for (B11-RV finding).
        raise ValueError(
            "load_runtime_config[opencode_server]: remote_budget must be a "
            f"mapping, got {type(remote_budget_payload).__name__}: "
            f"{remote_budget_payload!r} "
            "(expected a block with max_requests_per_chapter and optional "
            "max_retry_requests_per_chapter / max_wait_seconds_on_rate_limit "
            "/ max_reported_cost; omit the key or use null for the 500 default)"
        )
    # FAIL-CLOSED on unsupported keys: every key that can change the model
    # answer must either be deserialized or rejected loudly — silently dropping
    # a field would let a config claim one identity while the backend runs with
    # another's defaults (determinism/auditability violation). Known keys are
    # enumerated explicitly; any other top-level key is rejected.
    _allowed_opencode_keys = {
        "kind", "server_mode", "managed", "base_url",
        "server_version_policy", "pinned_server_version", "auth",
        "remote_budget", "model_bindings", "structured_output_mode",
        "structured_output_retry_count", "default_temperature",
        "default_max_output_tokens", "timeout_seconds", "http_retries",
        "retry_delay_seconds", "agent", "system_prompt",
        "system_prompt_version", "session_scope", "retain_success_sessions",
        "retain_failed_sessions", "tools_disabled", "reasoning_effort_map",
        "reasoning",
    }
    _unknown = set(payload) - _allowed_opencode_keys
    if _unknown:
        raise ValueError(
            "load_runtime_config[opencode_server]: unsupported key(s) "
            f"{sorted(_unknown)} — add deserialization or remove the key"
        )
    # Optional session/retry/tool policy fields: deserialize when present,
    # otherwise keep the dataclass defaults (preserves existing identity).
    reasoning_effort_map = None
    if "reasoning_effort_map" in payload and payload["reasoning_effort_map"] is not None:
        raw_map = payload["reasoning_effort_map"]
        if not isinstance(raw_map, Mapping):
            raise ValueError(
                "load_runtime_config[opencode_server]: reasoning_effort_map must be "
                f"a mapping, got {type(raw_map).__name__}"
            )
        try:
            reasoning_effort_map = {int(k): str(v) for k, v in raw_map.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "load_runtime_config[opencode_server]: reasoning_effort_map keys "
                f"must be int levels 1/2/3, got {raw_map!r}: {exc}"
            ) from exc
    server_kwargs: dict = dict(
        base_url=str(payload.get("base_url", "http://127.0.0.1:4096")),
        server_version_policy=str(
            payload.get("server_version_policy", "compatible_minor")
        ),
        pinned_server_version=str(
            payload.get("pinned_server_version", OPENCODE_PINNED_SERVER_VERSION)
        ),
        username_env=str(username_env or "OPENCODE_SERVER_USERNAME"),
        password_env=str(password_env or "OPENCODE_SERVER_PASSWORD"),
        model_bindings=dict(payload.get("model_bindings") or {}),
        structured_output_mode=str(
            payload.get("structured_output_mode", "prompt_only")
        ),
        default_temperature=payload.get("default_temperature"),
        default_max_output_tokens=payload.get("default_max_output_tokens"),
        timeout_seconds=float(
            payload.get("timeout_seconds", OpenCodeServerBackendConfig().timeout_seconds)
        ),
    )
    if "agent" in payload:
        server_kwargs["agent"] = payload["agent"]
    if "system_prompt" in payload:
        server_kwargs["system_prompt"] = str(payload["system_prompt"])
    if "system_prompt_version" in payload:
        server_kwargs["system_prompt_version"] = str(payload["system_prompt_version"])
    if "session_scope" in payload:
        server_kwargs["session_scope"] = str(payload["session_scope"])
    if "retain_success_sessions" in payload:
        server_kwargs["retain_success_sessions"] = bool(payload["retain_success_sessions"])
    if "retain_failed_sessions" in payload:
        server_kwargs["retain_failed_sessions"] = bool(payload["retain_failed_sessions"])
    if "tools_disabled" in payload:
        server_kwargs["tools_disabled"] = bool(payload["tools_disabled"])
    if "structured_output_retry_count" in payload:
        server_kwargs["structured_output_retry_count"] = int(payload["structured_output_retry_count"])
    if "http_retries" in payload:
        server_kwargs["http_retries"] = int(payload["http_retries"])
    if "retry_delay_seconds" in payload:
        server_kwargs["retry_delay_seconds"] = float(payload["retry_delay_seconds"])
    # Profile-bearing reasoning default (identity-bearing, 0-3).
    # When present, CLI omission uses profile value; explicit CLI overrides.
    if "reasoning" in payload and payload["reasoning"] is not None:
        raw_reasoning = payload["reasoning"]
        if isinstance(raw_reasoning, bool):
            raise ValueError(
                f"load_runtime_config[opencode_server]: reasoning must be an integer 0-3, got {raw_reasoning!r} (bool is not valid)"
            )
        if not isinstance(raw_reasoning, int):
            raise ValueError(
                f"load_runtime_config[opencode_server]: reasoning must be an integer 0-3, got {raw_reasoning!r}"
            )
        if raw_reasoning not in (0, 1, 2, 3):
            raise ValueError(
                f"load_runtime_config[opencode_server]: reasoning must be 0-3, got {raw_reasoning!r}"
            )
        server_kwargs["reasoning"] = int(raw_reasoning)
    if reasoning_effort_map is not None:
        server_kwargs["reasoning_effort_map"] = reasoning_effort_map
    if remote_budget is not None:
        # None keeps the dataclass default (``RemoteBudget()``); only an
        # explicit ``remote_budget`` YAML block overrides it.
        server_kwargs["remote_budget"] = remote_budget
    server = OpenCodeServerBackendConfig(**server_kwargs)
    return OpenCodeBackendConfig(
        server=server,
        server_mode=str(payload.get("server_mode", "external")),
        managed=managed_spec,
    )


def _load_composite(payload: Mapping[str, Any]) -> CompositeBackendConfig:
    backends = {
        name: load_runtime_config(sub)
        for name, sub in (payload.get("backends") or {}).items()
    }
    role_backend_map = dict(payload.get("role_backend_map") or {})
    if not backends:
        raise ValueError("load_runtime_config[composite]: backends required")
    if not role_backend_map:
        raise ValueError(
            "load_runtime_config[composite]: role_backend_map required"
        )
    return CompositeBackendConfig(
        backends=backends,
        role_backend_map=role_backend_map,
    )


# ---------------------------------------------------------------------------
# Offline host-local preflight (host filesystem/port/env checks only)
# ---------------------------------------------------------------------------

import json as _json
import os as _os
import socket as _socket


@dataclass(frozen=True)
class PreflightCheck:
    """One host-local check result (never contains credential values)."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    """Sanitized, offline preflight result for a resolved runtime profile."""

    ok: bool
    kind: str
    identity_hash: str
    public_record: Mapping[str, Any]
    model_bindings: Mapping[str, str]
    effective_options: Mapping[str, Any]
    checks: Sequence[PreflightCheck]
    errors: Sequence[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "identity_hash": self.identity_hash,
            "public_record": dict(self.public_record),
            "model_bindings": dict(self.model_bindings),
            "effective_options": dict(self.effective_options),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "errors": list(self.errors),
        }

    def to_json(self) -> str:
        return _json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def format_human(self) -> str:
        lines: List[str] = []
        lines.append(f"Runtime preflight: {'PASS' if self.ok else 'FAIL'}")
        lines.append(f"  kind: {self.kind}")
        lines.append(f"  identity: {self.identity_hash}")
        if self.model_bindings:
            lines.append(f"  bindings: {dict(self.model_bindings)}")
        if self.effective_options:
            # Only top-level effective_options keys for readability
            lines.append(f"  policy: {dict(self.effective_options)}")
        lines.append("  checks:")
        for c in self.checks:
            status = "OK" if c.ok else "FAIL"
            lines.append(f"    - {c.name}: {status} {c.detail}")
        if self.errors:
            lines.append("  errors:")
            for e in self.errors:
                lines.append(f"    - {e}")
        return "\n".join(lines)


def _preflight_check_local(cfg: LocalLlamaBackendConfig) -> List[PreflightCheck]:
    checks: List[PreflightCheck] = []
    exe = Path(cfg.exe)
    if exe.is_file():
        checks.append(PreflightCheck(name=f"exe {exe}", ok=True, detail="present"))
    else:
        checks.append(PreflightCheck(name=f"exe {exe}", ok=False, detail="missing or not a file"))
    for key in sorted(cfg.model_paths):
        p = Path(cfg.model_paths[key])
        if p.is_file():
            checks.append(PreflightCheck(name=f"model_path {key}:{p}", ok=True, detail="present"))
        else:
            checks.append(PreflightCheck(name=f"model_path {key}:{p}", ok=False, detail="missing or not a file"))
    # Port range already validated at load; here check host/port readiness without starting server
    try:
        port_ok = 1 <= int(cfg.port) <= 65535
    except Exception:
        port_ok = False
    if port_ok:
        # Try to see if port is obviously invalid on localhost without binding aggressively.
        # We attempt a non-blocking bind to detect EADDRINUSE, but treat any failure conservatively as FAIL.
        # Select address family to match host: ::1 requires AF_INET6, otherwise AF_INET (preserves safe-local-host validation).
        try:
            bind_host = cfg.host if cfg.host not in ("", "0.0.0.0") else "127.0.0.1"
            is_ipv6 = ":" in bind_host
            family = _socket.AF_INET6 if is_ipv6 else _socket.AF_INET
            with _socket.socket(family, _socket.SOCK_STREAM) as s:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                if is_ipv6:
                    s.bind((bind_host, int(cfg.port), 0, 0))
                else:
                    s.bind((bind_host, int(cfg.port)))
            checks.append(PreflightCheck(name=f"port {cfg.host}:{cfg.port}", ok=True, detail="available"))
        except OSError as exc:
            # If the port is already in use or host invalid, report as FAIL but do not start server.
            checks.append(PreflightCheck(name=f"port {cfg.host}:{cfg.port}", ok=False, detail=f"unavailable: {exc.strerror or exc}"))
    else:
        checks.append(PreflightCheck(name=f"port {cfg.host}:{cfg.port}", ok=False, detail="invalid port range"))
    return checks


def _preflight_check_opencode(cfg: OpenCodeBackendConfig) -> List[PreflightCheck]:
    checks: List[PreflightCheck] = []
    for env_name in (cfg.server.username_env, cfg.server.password_env):
        val = _os.environ.get(env_name)
        if val:
            checks.append(PreflightCheck(name=f"env {env_name}", ok=True, detail="present"))
        else:
            checks.append(PreflightCheck(name=f"env {env_name}", ok=False, detail="missing or empty"))
    # Config syntax already validated at load; report that as a check
    checks.append(PreflightCheck(name="config syntax", ok=True, detail="valid"))
    return checks


def run_runtime_preflight(
    cfg: BackendRuntimeConfig,
    *,
    reasoning: Optional[int] = None,
) -> PreflightReport:
    """Offline, host-local preflight for a resolved runtime config.

    No server is started, no network request is made, no artifacts are
    created, and no credential values are read or emitted — only the
    presence of required environment variables is reported by name.
    The ``cfg`` should already include any translator/reviewer overrides
    so the reported identity/bindings are exactly what the run would use.
    """
    errors: List[str] = []
    checks: List[PreflightCheck] = []
    if reasoning is not None:
        try:
            validate_reasoning_backend(reasoning, cfg)
            checks.append(PreflightCheck(name=f"reasoning {reasoning}", ok=True, detail="compatible with generator backend"))
        except ValueError as exc:
            errors.append(str(exc))
            checks.append(PreflightCheck(name=f"reasoning {reasoning}", ok=False, detail=str(exc)))
    # Collect host checks per kind
    if isinstance(cfg, LocalLlamaBackendConfig):
        checks.extend(_preflight_check_local(cfg))
    elif isinstance(cfg, OpenCodeBackendConfig):
        checks.extend(_preflight_check_opencode(cfg))
    elif isinstance(cfg, CompositeBackendConfig):
        for name in sorted(cfg.backends):
            sub = cfg.backends[name]
            sub_checks: List[PreflightCheck] = []
            if isinstance(sub, LocalLlamaBackendConfig):
                sub_checks = _preflight_check_local(sub)
            elif isinstance(sub, OpenCodeBackendConfig):
                sub_checks = _preflight_check_opencode(sub)
            else:
                sub_checks = [PreflightCheck(name=f"backend {name}", ok=True, detail="unknown kind, no host checks")]
            for c in sub_checks:
                checks.append(PreflightCheck(name=f"{name}/{c.name}", ok=c.ok, detail=c.detail))
    else:
        errors.append(f"unknown config kind {type(cfg).__name__}")
    ok = all(c.ok for c in checks) and not errors
    descriptor = cfg.build_descriptor()
    return PreflightReport(
        ok=ok,
        kind=descriptor.kind,
        identity_hash=descriptor.identity_hash,
        public_record=descriptor.public_record(),
        model_bindings=dict(descriptor.model_bindings or {}),
        effective_options=dict(descriptor.effective_options or {}),
        checks=tuple(checks),
        errors=tuple(errors),
    )


__all__ = [
    "LOCAL_LLAMA_TRANSPORT_VERSION",
    "ENDPOINT_FAMILY_COMPOSITE",
    "COMPOSITE_TRANSPORT_VERSION",
    "ROLE_GENERATOR",
    "ROLE_FIDELITY_REVIEWER",
    "ROLE_RUSSIAN_SELECTOR",
    "ROLE_QWEN_AUDIT",
    "ROLE_GEMMA_AUDIT",
    "ROLE_FORMATTING",
    "ROLE_REPAIR",
    "ROLE_ENTITY_EXTRACTOR",
    "TRANSLATOR_ROLES",
    "REVIEWER_ROLES",
    "BackendRuntimeConfig",
    "LocalLlamaBackendConfig",
    "StrictBackendConfig",
    "OpenCodeBackendConfig",
    "LocalRoutingBackend",
    "CompositeCompletionBackend",
    "CompositeBackendConfig",
    "ProviderModel",
    "ProvidersRegistry",
    "REASONING_EFFORT_LADDER",
    "DEFAULT_REASONING_EFFORT",
    "load_providers_registry",
    "resolve_role_model",
    "nearest_declared_effort",
    "build_reasoning_effort_map",
    "apply_role_models",
    "apply_provider_flags",
    "load_runtime_config",
    "build_role_backend",
    "build_role_adapters",
    "build_repair_adapters",
    "JsonRetryPolicy",
    "PreflightCheck",
    "PreflightReport",
    "run_runtime_preflight",
    "SUPPORTED_LOCAL_MODEL_KEYS",
]
