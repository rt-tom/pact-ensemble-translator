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
from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.opencode_backend import (
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
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
        for name, backend in self._sub.items():
            bindings = backend.descriptor.model_bindings or {}
            for ref in set(bindings.values()):
                if ref:
                    self._ref_to_name[ref] = name

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        name = self._ref_to_name.get(request.model_ref)
        if name is None:
            raise CompletionError(
                "CompositeCompletionBackend: no backend serves model_ref "
                f"{request.model_ref!r} (known: {sorted(self._ref_to_name)})"
            )
        return self._sub[name].complete(request)

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
        bindings: Dict[str, str] = {}
        for cfg in self.backends.values():
            for role, ref in (cfg.build_descriptor().model_bindings or {}).items():
                if ref and role not in bindings:
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
        # Composite shape limitation (PR #107 review): only the FIRST
        # LocalLifecycleCoordinator and FIRST RemoteRuntimeCoordinator are
        # kept for event accounting/summary; any additional sub-backend of
        # the same kind is still closed on close() but its switch/call
        # events never reach runtime.events_since()/summary(). Today the
        # only config kinds are local_llama and opencode_server, so a
        # realistic composite is 1-local + 1-remote; if a future profile
        # needs 2-local or 2-remote sub-backends, the coordinator must
        # merge multiple event sources instead of discarding the extras.
        sub_backends: Dict[str, CompletionBackend] = {}
        local_coord: Optional[LocalLifecycleCoordinator] = None
        remote_coord: Optional[RemoteRuntimeCoordinator] = None
        for name, cfg in self.backends.items():
            runtime = cfg.build_runtime(log_dir=log_dir)
            if isinstance(runtime, LocalLifecycleCoordinator):
                if local_coord is None:
                    local_coord = runtime
                sub_backends[name] = LocalRoutingBackend(runtime.router, cfg)
            elif isinstance(runtime, RemoteRuntimeCoordinator):
                if remote_coord is None:
                    remote_coord = runtime
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
        return CompositeRuntimeCoordinator(
            local_coord, remote_coord, descriptor, backend=composite_backend,
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
        return LocalRoutingBackend(runtime.router, cfg)
    if isinstance(cfg, OpenCodeBackendConfig):
        return runtime.backend
    if isinstance(cfg, CompositeBackendConfig):
        return runtime.backend
    raise TypeError(
        f"build_role_backend: unsupported config {type(cfg).__name__}"
    )


def build_role_adapters(
    cfg: BackendRuntimeConfig, runtime: RuntimeCoordinator
) -> Tuple[Any, Any, Any, Any, Any]:
    """The five role adapters ``run_chapter_strict`` needs injected.

    Return order matches ``build_strict_lifecycle``: ``(model_caller,
    qwen_evaluator, gemma_selector, qwen_audit_evaluator,
    gemma_audit_evaluator)``. Imported lazily so ``runtime_config`` stays
    importable without ``backend_role_adapters`` (no import cycle).
    """
    from pact_v4.runtime.backend_role_adapters import (
        BackendGemmaAuditEvaluator,
        BackendGemmaSelector,
        BackendModelCaller,
        BackendQwenAuditEvaluator,
        BackendQwenEvaluator,
    )

    backend = build_role_backend(cfg, runtime)
    return (
        BackendModelCaller(backend),
        BackendQwenEvaluator(backend),
        BackendGemmaSelector(backend),
        BackendQwenAuditEvaluator(backend),
        BackendGemmaAuditEvaluator(backend),
    )


def build_repair_adapters(
    cfg: BackendRuntimeConfig, runtime: RuntimeCoordinator
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
    """
    from pact_v4.runtime.backend_role_adapters import (
        BackendGemmaAuditEvaluator,
        BackendQwenAuditEvaluator,
        BackendRegionFidelityGate,
        BackendRepairCaller,
    )

    backend = build_role_backend(cfg, runtime)
    return (
        BackendRepairCaller(backend),
        BackendRegionFidelityGate(backend),
        BackendQwenAuditEvaluator(backend),
        BackendGemmaAuditEvaluator(backend),
    )


def build_formatting_adapters(
    cfg: BackendRuntimeConfig, runtime: RuntimeCoordinator
) -> Tuple[Any]:
    """The Phase 5 formatting callable ``run_chapter_strict`` needs injected.

    Return order: ``(formatting_caller,)`` — a single ``BackendFormattingCaller``
    over the coordinator ``CompletionBackend`` (``build_role_backend``), never
    a local lifecycle adapter. Phase 5's model fallback tier must run through
    the same backend-neutral boundary in local, remote and composite profiles
    (dual-mode rule; no retrofit needed). Imported lazily to avoid an import
    cycle with ``backend_role_adapters``.
    """
    from pact_v4.runtime.backend_role_adapters import BackendFormattingCaller

    backend = build_role_backend(cfg, runtime)
    return (BackendFormattingCaller(backend),)


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


def _load_local(payload: Mapping[str, Any]) -> LocalLlamaBackendConfig:
    model_paths = {k: Path(v) for k, v in (payload.get("model_paths") or {}).items()}
    model_names = dict(payload.get("model_names") or {})
    server_args = {k: list(v) for k, v in (payload.get("server_args") or {}).items()}
    if not model_paths:
        raise ValueError("load_runtime_config[local_llama]: model_paths required")
    return LocalLlamaBackendConfig(
        exe=Path(payload["exe"]),
        device=payload.get("device", "SYCL0"),
        host=payload.get("host", "127.0.0.1"),
        model_paths=model_paths,
        model_names=model_names,
        server_args=server_args,
        port=int(payload.get("port", 8093)),
        startup_timeout=float(payload.get("startup_timeout", 240.0)),
        unload_timeout=float(payload.get("unload_timeout", 30.0)),
    )


def _load_opencode(payload: Mapping[str, Any]) -> OpenCodeBackendConfig:
    auth = payload.get("auth") or {}
    username_env = auth.get("username_env") if isinstance(auth, Mapping) else None
    password_env = auth.get("password_env") if isinstance(auth, Mapping) else None
    managed = payload.get("managed")
    managed_spec: Optional[ManagedServerSpec] = None
    if isinstance(managed, Mapping):
        managed_spec = ManagedServerSpec(
            hostname=managed.get("hostname", DEFAULT_HOSTNAME),
            port=int(managed.get("port", 4096)),
            pinned_server_version=managed.get(
                "pinned_server_version", payload.get("pinned_server_version", "1.4.7")
            ),
            server_version_policy=managed.get(
                "server_version_policy",
                payload.get("server_version_policy", "compatible_minor"),
            ),
        )
    server = OpenCodeServerBackendConfig(
        base_url=str(payload.get("base_url", "http://127.0.0.1:4096")),
        server_version_policy=str(
            payload.get("server_version_policy", "compatible_minor")
        ),
        pinned_server_version=str(
            payload.get("pinned_server_version", "1.4.7")
        ),
        username_env=str(username_env or "OPENCODE_SERVER_USERNAME"),
        password_env=str(password_env or "OPENCODE_SERVER_PASSWORD"),
        model_bindings=dict(payload.get("model_bindings") or {}),
        structured_output_mode=str(
            payload.get("structured_output_mode", "prompt_only")
        ),
        default_temperature=payload.get("default_temperature"),
        default_max_output_tokens=payload.get("default_max_output_tokens"),
    )
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
    "BackendRuntimeConfig",
    "LocalLlamaBackendConfig",
    "StrictBackendConfig",
    "OpenCodeBackendConfig",
    "LocalRoutingBackend",
    "CompositeCompletionBackend",
    "CompositeBackendConfig",
    "load_runtime_config",
    "build_role_backend",
    "build_role_adapters",
    "build_repair_adapters",
    "build_formatting_adapters",
]
