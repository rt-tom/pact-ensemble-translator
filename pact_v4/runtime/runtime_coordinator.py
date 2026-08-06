"""Runtime coordinators: make ``run_chapter_strict`` backend-agnostic.

V4 C2 / PR 3 of the OpenCode integration plan
(``docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md`` §9.1):

.. code-block:: python

    class RuntimeCoordinator(Protocol):
        @property
        def backend_descriptor(self) -> BackendDescriptor: ...
        def event_count(self) -> int: ...
        def events_since(self, index: int) -> Sequence[BackendEvent]: ...
        def close(self) -> None: ...
        def summary(self) -> Mapping[str, Any]: ...

The strict driver previously depended on a concrete ``ModelRouter``
(``router.switches`` / ``router.current_model`` / ``router.release()``).
A ``RuntimeCoordinator`` replaces that narrow slice of the router's
surface so the same driver can run Phase 1-2 against a local
``llama-server``, a remote ``opencode serve``, or a mixed profile:

* ``LocalLifecycleCoordinator`` adapts the existing ``ModelRouter``
  (switch events are the local lifecycle's stop/start record);
* ``RemoteRuntimeCoordinator`` makes no model swaps; its events are the
  backend's per-call usage records, aggregated in ``summary()``;
* ``CompositeRuntimeCoordinator`` combines a local switch source and a
  remote call source into one event sequence and a combined summary.

Design rules (plan §9.1, §9.3):

* ``event_count()`` / ``events_since()`` are lazily synced from the
  underlying router/backend call records, so the coordinator observes
  events even though the model-call adapters drive them directly.
* ``summary()`` returns ``{"local_lifecycle": ...|None,
  "remote_calls": ...|None}``; usage/cost are ``null`` when the provider
  did not report them (never invented).
* ``close()`` is idempotent and only stops what the coordinator owns: a
  local router's resident model or the remote backend's HTTP session /
  its own OpenCode sessions. It never stops a foreign server.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Protocol, Sequence

from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
)
from pact_v4.runtime.model_lifecycle import ModelRouter, SwitchRecord

LOG = logging.getLogger(__name__)

# Event kinds in a coordinator's flat event sequence.
EVENT_KIND_LOCAL_SWITCH = "local_switch"
EVENT_KIND_REMOTE_CALL = "remote_call"


@dataclass(frozen=True)
class BackendEvent:
    """One backend lifecycle event (local switch or remote call).

    ``kind`` discriminates the two: local switch events carry
    ``to_model``/timing; remote call events carry ``model_ref``/``usage``.
    JSON-serialisable via ``to_payload()`` for journal/record provenance.
    """

    kind: str
    label: str
    to_model: Optional[str] = None
    model_ref: Optional[str] = None
    cold_acquire_seconds: Optional[float] = None
    unload_seconds: Optional[float] = None
    load_retries: Optional[int] = None
    peak_vram_mb: Optional[float] = None
    timestamp: str = ""
    wall_seconds: float = 0.0
    usage: Mapping[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    finish_reason: Optional[str] = None
    retry_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", dict(self.usage))

    def to_payload(self) -> Mapping[str, Any]:
        """Sanitized JSON-safe view (no credentials ever enter events)."""
        return {
            "kind": self.kind,
            "label": self.label,
            "to_model": self.to_model,
            "model_ref": self.model_ref,
            "cold_acquire_seconds": self.cold_acquire_seconds,
            "unload_seconds": self.unload_seconds,
            "load_retries": self.load_retries,
            "peak_vram_mb": self.peak_vram_mb,
            "timestamp": self.timestamp,
            "wall_seconds": self.wall_seconds,
            "usage": dict(self.usage),
            "request_id": self.request_id,
            "session_id": self.session_id,
            "finish_reason": self.finish_reason,
            "retry_count": self.retry_count,
        }


class RuntimeCoordinator(Protocol):
    """Narrow backend-lifecycle surface consumed by ``run_chapter_strict``.

    Implementations: ``LocalLifecycleCoordinator``,
    ``RemoteRuntimeCoordinator``, ``CompositeRuntimeCoordinator``.

    ``release()`` is the non-terminal cleanup that runs between phases
    (e.g. Phase 1-2 -> Step 6): it frees a local resident model so a
    single-resident run can re-acquire it, and is a no-op for backends
    without a resident model. ``close()`` is terminal (closes the remote
    HTTP session / deletes owned OpenCode sessions / stops a managed
    server) and is called once at the very end of the run.
    """

    @property
    def backend_descriptor(self) -> BackendDescriptor: ...

    def event_count(self) -> int: ...

    def events_since(self, index: int) -> Sequence[BackendEvent]: ...

    def release(self) -> None: ...

    def close(self) -> None: ...

    def summary(self) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def switch_aggregates_by_model(switches: List[SwitchRecord]) -> dict:
    """Per-model lifecycle aggregates in the same shape Measurement 2 used."""
    by_model: dict = {}
    for sw in switches:
        bucket = by_model.setdefault(sw.to_model, {
            "cold_acquire_seconds": [], "unload_seconds": [], "peak_vram_mb": [],
        })
        bucket["cold_acquire_seconds"].append(sw.cold_acquire_seconds)
        if sw.unload_seconds is not None:
            bucket["unload_seconds"].append(sw.unload_seconds)
        if sw.peak_vram_mb is not None:
            bucket["peak_vram_mb"].append(sw.peak_vram_mb)
    out: dict = {}
    for model_key, fields in by_model.items():
        out[model_key] = {
            name: {
                "n": len(values),
                "median": statistics.median(values) if values else None,
                "p95": _percentile(values, 0.95),
            }
            for name, values in fields.items()
        }
    return out


def switch_payload(sw: SwitchRecord) -> Mapping[str, Any]:
    """``SwitchRecord`` in the same dict shape v1 records used."""
    return {
        "from_model": sw.from_model,
        "to_model": sw.to_model,
        "cold_acquire_seconds": sw.cold_acquire_seconds,
        "unload_seconds": sw.unload_seconds,
        "load_retries": sw.load_retries,
        "peak_vram_mb": sw.peak_vram_mb,
        "timestamp": sw.timestamp,
    }


def local_lifecycle_summary(switches: List[SwitchRecord]) -> dict:
    """The ``local_lifecycle`` block of ``summary()`` for a local source."""
    return {
        "startup_count": len(switches),
        "restart_count": max(0, len(switches) - 1) if switches else 0,
        "switches": [switch_payload(sw) for sw in switches],
        "aggregates_by_model": switch_aggregates_by_model(switches),
    }


def remote_calls_summary(records: Sequence[BackendCallRecord]) -> dict:
    """The ``remote_calls`` block of ``summary()`` for a remote source.

    Only values actually reported by the provider are summed; if no call
    reported a cost, ``reported_cost`` stays ``None`` (plan §9.3: never a
    computed guess).
    """
    count = len(records)
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    costs: List[float] = []
    for rec in records:
        usage = rec.usage if isinstance(rec.usage, Mapping) else {}
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        cached_input_tokens += int(usage.get("cached_input_tokens") or 0)
        cost = usage.get("reported_cost")
        if cost is not None:
            try:
                costs.append(float(cost))
            except (TypeError, ValueError):
                pass
    return {
        "count": count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reported_cost": round(sum(costs), 6) if costs else None,
    }


# ---------------------------------------------------------------------------
# Event conversions
# ---------------------------------------------------------------------------


def _switch_event(sw: SwitchRecord) -> BackendEvent:
    return BackendEvent(
        kind=EVENT_KIND_LOCAL_SWITCH,
        label=f"switch_to_{sw.to_model}",
        to_model=sw.to_model,
        cold_acquire_seconds=sw.cold_acquire_seconds,
        unload_seconds=sw.unload_seconds,
        load_retries=sw.load_retries,
        peak_vram_mb=sw.peak_vram_mb,
        timestamp=sw.timestamp,
    )


def _call_event(rec: BackendCallRecord) -> BackendEvent:
    return BackendEvent(
        kind=EVENT_KIND_REMOTE_CALL,
        label=rec.label,
        model_ref=rec.model_ref,
        wall_seconds=rec.wall_seconds,
        usage=dict(rec.usage) if isinstance(rec.usage, Mapping) else {},
        request_id=rec.request_id,
        session_id=rec.session_id,
        finish_reason=rec.finish_reason,
        retry_count=rec.retry_count,
    )


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


class LocalLifecycleCoordinator:
    """Adapts the existing single-resident ``ModelRouter``.

    Events are the router's switch records; ``summary()`` reports the
    ``local_lifecycle`` block; ``close()`` releases the resident model
    (only the one this router owns).
    """

    def __init__(
        self,
        router: ModelRouter,
        descriptor: Optional[BackendDescriptor] = None,
    ) -> None:
        self._router = router
        self._descriptor = descriptor
        self._events: List[BackendEvent] = []
        self._closed = False

    @property
    def router(self) -> ModelRouter:
        return self._router

    def _sync(self) -> None:
        switches = self._router.switches
        while len(self._events) < len(switches):
            self._events.append(_switch_event(switches[len(self._events)]))

    @property
    def backend_descriptor(self) -> BackendDescriptor:
        if self._descriptor is None:
            raise ValueError(
                "LocalLifecycleCoordinator: no BackendDescriptor provided"
            )
        return self._descriptor

    def event_count(self) -> int:
        self._sync()
        return len(self._events)

    def events_since(self, index: int) -> Sequence[BackendEvent]:
        self._sync()
        return self._events[index:]

    def local_switch_event_indices(self, start: int) -> List[int]:
        """Global indices in ``[start, event_count())`` that are local switches."""
        self._sync()
        return list(range(start, len(self._events)))

    def release(self) -> None:
        """Free the resident model (non-terminal; the router can re-acquire).

        Called between Phase 1-2 and Step 6 so a local single-resident run
        frees VRAM without permanently closing the backend.
        """
        self._sync()
        if self._router.current_model is not None:
            self._router.release()

    def close(self) -> None:
        self._sync()
        if self._closed:
            return
        self._closed = True
        if self._router.current_model is not None:
            self._router.release()

    def summary(self) -> Mapping[str, Any]:
        self._sync()
        return {
            "local_lifecycle": local_lifecycle_summary(self._router.switches),
            "remote_calls": None,
        }


# ---------------------------------------------------------------------------
# Remote
# ---------------------------------------------------------------------------


class RemoteRuntimeCoordinator:
    """Backend-lifecycle coordinator over a single remote ``CompletionBackend``.

    No model swaps: events are the backend's per-call records, and
    ``summary()`` aggregates reported usage/cost. ``close()`` closes the
    backend (HTTP session + only its own OpenCode sessions) and then runs
    any ``add_cleanup`` hooks (e.g. stopping a managed ``opencode serve``
    this coordinator started).
    """

    def __init__(self, backend: CompletionBackend) -> None:
        self._backend = backend
        self._events: List[BackendEvent] = []
        self._cleanup: List[Any] = []
        self._closed = False

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def set_usage_writer(self, writer: Any) -> None:
        """Attach a usage writer to the backend's per-call completion sink.

        The ``OpenCodeServerBackend`` writes each completed remote call
        (success and failure) into ``usage.ndjson`` at the exact moment the
        call finishes — not at phase boundaries — so a crash inside a phase
        still leaves the already-completed calls in the artifact. Each
        backend materializes each call exactly once, so a resumed run (a
        fresh backend) appends only new calls, never duplicates.
        """
        sink = getattr(self._backend, "set_usage_sink", None)
        if sink is not None:
            sink(writer.write_call)

    def add_cleanup(self, fn: Any) -> None:
        """Register a teardown callback run by ``close()`` (idempotent hooks)."""
        self._cleanup.append(fn)

    def _sync(self) -> None:
        records = self._backend.call_records()
        while len(self._events) < len(records):
            self._events.append(_call_event(records[len(self._events)]))

    @property
    def backend_descriptor(self) -> BackendDescriptor:
        return self._backend.descriptor

    def event_count(self) -> int:
        self._sync()
        return len(self._events)

    def events_since(self, index: int) -> Sequence[BackendEvent]:
        self._sync()
        return self._events[index:]

    def local_switch_event_indices(self, start: int) -> List[int]:
        return []

    def release(self) -> None:
        """No-op: a remote backend has no resident model to free between phases."""

    def close(self) -> None:
        self._sync()
        if self._closed:
            return
        self._closed = True
        try:
            self._backend.close()
        finally:
            for fn in self._cleanup:
                try:
                    fn()
                except Exception:  # noqa: BLE001 -- teardown hooks are best-effort
                    LOG.warning("RemoteRuntimeCoordinator: cleanup hook failed: %s", fn)

    def summary(self) -> Mapping[str, Any]:
        self._sync()
        return {
            "local_lifecycle": None,
            "remote_calls": remote_calls_summary(self._backend.call_records()),
        }


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


class CompositeRuntimeCoordinator:
    """Combines a local switch source and a remote call source.

    The unified event sequence is ordered local-first then remote (stable
    per-run ordering, deterministic for provenance; the plan's §9.1
    requires the two index families, not a strict wall-clock merge).
    ``summary()`` reports both blocks; ``close()`` closes both.

    ``backend`` (optional, PR 4) is the role-routing ``CompletionBackend``
    the composite profile owns — the same ``CompositeCompletionBackend``
    built by ``CompositeBackendConfig.build_runtime`` so the ``Backend*``
    role adapters can route Phase 1-2/Step 6 calls to the right sub-backend.
    It is exposed read-only via the ``backend`` property (raises when no
    backend was attached, so the coordinator can still be constructed as a
    pure event/telemetry holder as before).
    """

    def __init__(
        self,
        local: Optional[LocalLifecycleCoordinator],
        remote: Optional[RemoteRuntimeCoordinator],
        descriptor: BackendDescriptor,
        backend: Optional[CompletionBackend] = None,
    ) -> None:
        if local is None and remote is None:
            raise ValueError(
                "CompositeRuntimeCoordinator: need at least one sub-coordinator"
            )
        self._local = local
        self._remote = remote
        self._descriptor = descriptor
        self._backend = backend
        self._closed = False

    @property
    def local(self) -> Optional[LocalLifecycleCoordinator]:
        return self._local

    @property
    def remote(self) -> Optional[RemoteRuntimeCoordinator]:
        return self._remote

    @property
    def backend(self) -> CompletionBackend:
        if self._backend is None:
            raise ValueError(
                "CompositeRuntimeCoordinator: no composite CompletionBackend "
                "was attached"
            )
        return self._backend

    def set_usage_writer(self, writer: Any) -> None:
        """Forward the usage writer to the remote sub-coordinator.

        Local switch events are never written (the writer only accepts
        remote-call events), so a composite run's local calls stay in
        ``local_lifecycle`` exactly like a local-only run.
        """
        if self._remote is not None:
            self._remote.set_usage_writer(writer)

    @property
    def backend_descriptor(self) -> BackendDescriptor:
        return self._descriptor

    def event_count(self) -> int:
        return (self._local.event_count() if self._local else 0) + (
            self._remote.event_count() if self._remote else 0
        )

    def events_since(self, index: int) -> Sequence[BackendEvent]:
        local_count = self._local.event_count() if self._local else 0
        remote_count = self._remote.event_count() if self._remote else 0
        total = local_count + remote_count
        if not 0 <= index <= total:
            raise IndexError(
                f"CompositeRuntimeCoordinator: index {index} out of range "
                f"[0, {total}]"
            )
        events: List[BackendEvent] = []
        for i in range(index, total):
            if i < local_count:
                events.append(self._local.events_since(i)[0])
            else:
                events.append(self._remote.events_since(i - local_count)[0])
        return events

    def local_switch_event_indices(self, start: int) -> List[int]:
        local_count = self._local.event_count() if self._local else 0
        total = self.event_count()
        return [i for i in range(start, total) if i < local_count]

    def release(self) -> None:
        if self._local is not None:
            self._local.release()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._local is not None:
            self._local.close()
        if self._remote is not None:
            self._remote.close()

    def summary(self) -> Mapping[str, Any]:
        local = self._local.summary()["local_lifecycle"] if self._local else None
        remote = self._remote.summary()["remote_calls"] if self._remote else None
        return {"local_lifecycle": local, "remote_calls": remote}


__all__ = [
    "EVENT_KIND_LOCAL_SWITCH",
    "EVENT_KIND_REMOTE_CALL",
    "BackendEvent",
    "RuntimeCoordinator",
    "LocalLifecycleCoordinator",
    "RemoteRuntimeCoordinator",
    "CompositeRuntimeCoordinator",
    "local_lifecycle_summary",
    "remote_calls_summary",
    "switch_aggregates_by_model",
    "switch_payload",
]
