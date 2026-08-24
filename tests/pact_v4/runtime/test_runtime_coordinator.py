"""Tests for the ``RuntimeCoordinator`` implementations (V4 C2, plan §9.1).

Covers the local/remote/composite coordinators' event accounting, summary
shape (``local_lifecycle`` / ``remote_calls``), index helpers for the
journal's ``backend_event_indices``/``switch_indices``, and ``close()``
ownership (only the coordinator's own resources are stopped).
"""
from __future__ import annotations

from typing import Any, List, Mapping, Sequence, Tuple

from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionRequest,
    CompletionResponse,
)
from pact_v4.runtime.model_lifecycle import ModelRouter, SwitchRecord
from pact_v4.runtime.runtime_coordinator import (
    EVENT_KIND_LOCAL_SWITCH,
    EVENT_KIND_REMOTE_CALL,
    CompositeRuntimeCoordinator,
    LocalLifecycleCoordinator,
    RemoteRuntimeCoordinator,
)


class _FakeLifecycleAdapter:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self._running = False

    def start(self, model_key, profile, extra_args, retries=1):
        self.calls.append(("start", model_key))
        self._running = True
        return 1.5, 0

    def stop(self):
        self.calls.append(("stop", ""))
        self._running = False
        return 0.5, True, 0

    def sample_vram(self):
        return 1024 * 1024 * 100


def _make_router() -> ModelRouter:
    return ModelRouter(
        _FakeLifecycleAdapter(),
        role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
        role_args={"gemma": [], "qwen": []},
    )


def _make_descriptor(kind: str = "local_llama") -> BackendDescriptor:
    return BackendDescriptor(
        kind=kind,
        transport_version="test/v1",
        endpoint_family="test",
        public_endpoint="http://127.0.0.1:9",
        model_bindings={"generator": "gemma"},
        effective_options={},
    )


def _make_call_record(
    label: str, *, usage: Mapping[str, Any], model_ref: str = "opencode-go/x"
) -> BackendCallRecord:
    return BackendCallRecord(
        label=label,
        model_ref=model_ref,
        request_id=f"req_{label}",
        session_id=f"ses_{label}",
        retry_count=0,
        finish_reason="end_turn",
        usage=usage,
        wall_seconds=0.5,
        raw_metadata={},
    )


class _FakeBackend:
    """CompletionBackend whose call_records are scripted (no network)."""

    def __init__(self, records: Sequence[BackendCallRecord]) -> None:
        self._records = list(records)
        self.closed = False
        self.descriptor = _make_descriptor(kind="opencode_server")

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise AssertionError("FakeBackend.complete is not exercised here")

    def call_records(self) -> Sequence[BackendCallRecord]:
        return list(self._records)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------


def test_local_coordinator_tracks_switches_and_releases_on_close():
    router = _make_router()
    coordinator = LocalLifecycleCoordinator(router, descriptor=_make_descriptor())
    assert coordinator.event_count() == 0

    router.ensure_resident("gemma")
    router.ensure_resident("qwen")
    router.ensure_resident("qwen")  # no-op, same model resident

    assert coordinator.event_count() == 2
    events = coordinator.events_since(0)
    assert [e.kind for e in events] == [
        EVENT_KIND_LOCAL_SWITCH, EVENT_KIND_LOCAL_SWITCH,
    ]
    assert [e.to_model for e in events] == ["gemma", "qwen"]
    # Journal index helpers: all events are local switches.
    assert coordinator.local_switch_event_indices(0) == [0, 1]
    assert coordinator.local_switch_event_indices(1) == [1]

    summary = coordinator.summary()
    assert summary["remote_calls"] is None
    lifecycle = summary["local_lifecycle"]
    assert lifecycle["startup_count"] == 2
    assert lifecycle["restart_count"] == 1
    assert lifecycle["aggregates_by_model"]["gemma"]["cold_acquire_seconds"]["n"] == 1

    assert router.current_model == "qwen"
    coordinator.close()
    assert router.current_model is None
    assert coordinator.close() is None  # idempotent


def test_local_coordinator_events_are_synced_lazily():
    router = _make_router()
    coordinator = LocalLifecycleCoordinator(router, descriptor=_make_descriptor())
    router.ensure_resident("gemma")
    # No explicit sync call: reading the count must observe the switch.
    assert coordinator.event_count() == 1
    assert coordinator.events_since(0)[0].to_model == "gemma"


# ---------------------------------------------------------------------------
# Remote
# ---------------------------------------------------------------------------


def test_remote_coordinator_aggregates_call_usage():
    records = [
        _make_call_record("a", usage={"input_tokens": 10, "output_tokens": 20,
                                       "cached_input_tokens": 3, "reported_cost": 0.01}),
        _make_call_record("b", usage={"input_tokens": 5, "output_tokens": 7,
                                       "cached_input_tokens": 0, "reported_cost": 0.02}),
    ]
    backend = _FakeBackend(records)
    coordinator = RemoteRuntimeCoordinator(backend)
    assert coordinator.event_count() == 2
    events = coordinator.events_since(0)
    assert [e.kind for e in events] == [EVENT_KIND_REMOTE_CALL] * 2
    assert [e.label for e in events] == ["a", "b"]
    # No local switches in a remote coordinator.
    assert coordinator.local_switch_event_indices(0) == []

    summary = coordinator.summary()
    assert summary["local_lifecycle"] is None
    remote = summary["remote_calls"]
    assert remote["count"] == 2
    assert remote["input_tokens"] == 15
    assert remote["output_tokens"] == 27
    assert remote["cached_input_tokens"] == 3
    assert remote["reported_cost"] == 0.03

    coordinator.close()
    assert backend.closed
    assert coordinator.close() is None  # idempotent


def test_remote_coordinator_reported_cost_null_when_absent():
    records = [
        _make_call_record("a", usage={"input_tokens": 1, "output_tokens": 2}),
        _make_call_record("b", usage={"input_tokens": 3, "output_tokens": 4}),
    ]
    coordinator = RemoteRuntimeCoordinator(_FakeBackend(records))
    remote = coordinator.summary()["remote_calls"]
    # Provider reported no cost -> null, never an invented guess.
    assert remote["reported_cost"] is None
    assert remote["input_tokens"] == 4


def test_remote_coordinator_cleanup_hooks_run_on_close():
    coordinator = RemoteRuntimeCoordinator(_FakeBackend([]))
    ran = []
    coordinator.add_cleanup(lambda: ran.append("hook"))
    coordinator.close()
    assert ran == ["hook"]


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def _composite(local: LocalLifecycleCoordinator, remote: RemoteRuntimeCoordinator):
    return CompositeRuntimeCoordinator(local, remote, _make_descriptor(kind="composite"))


def test_composite_coordinator_combines_events_and_summary():
    local_router = _make_router()
    local = LocalLifecycleCoordinator(local_router, descriptor=_make_descriptor())
    local_router.ensure_resident("gemma")

    remote_records = [
        _make_call_record("r1", usage={"input_tokens": 1, "output_tokens": 2,
                                        "cached_input_tokens": 0, "reported_cost": 0.5}),
    ]
    remote = RemoteRuntimeCoordinator(_FakeBackend(remote_records))
    composite = _composite(local, remote)

    assert composite.event_count() == 2
    events = composite.events_since(0)
    # Unified ordering is local-first then remote.
    assert events[0].kind == EVENT_KIND_LOCAL_SWITCH
    assert events[1].kind == EVENT_KIND_REMOTE_CALL
    # Only the local event is a "switch index" (local readers' view).
    assert composite.local_switch_event_indices(0) == [0]
    assert composite.local_switch_event_indices(1) == []

    summary = composite.summary()
    assert summary["local_lifecycle"]["startup_count"] == 1
    assert summary["remote_calls"]["count"] == 1
    assert summary["remote_calls"]["reported_cost"] == 0.5


def test_composite_coordinator_close_closes_both_sub_runtimes():
    local_router = _make_router()
    local = LocalLifecycleCoordinator(local_router, descriptor=_make_descriptor())
    local_router.ensure_resident("gemma")
    remote = RemoteRuntimeCoordinator(_FakeBackend([]))
    composite = _composite(local, remote)

    composite.close()
    assert local_router.current_model is None
    assert remote.backend.closed


def test_composite_multi_coordinator_sync_tracks_per_coordinator_offsets():
    """Regression: per-coordinator offsets avoid dup/omit when A emits after B."""
    # Two local coordinators with independent routers. Old aggregate-offset
    # _sync flattened locals into one list and sliced by total length, so
    # when coordinator A emitted after B the flattened tail duplicated B.
    r1 = _make_router()
    r2 = _make_router()
    c1 = LocalLifecycleCoordinator(r1, descriptor=_make_descriptor())
    c2 = LocalLifecycleCoordinator(r2, descriptor=_make_descriptor())
    r1.ensure_resident("gemma")
    r1.ensure_resident("qwen")  # c1: 2 events
    r2.ensure_resident("gemma")  # c2: 1 event
    composite_locals = CompositeRuntimeCoordinator(
        local=None, remote=None, descriptor=_make_descriptor(kind="composite"),
        locals=[c1, c2],
    )
    assert composite_locals.event_count() == 3
    first = list(composite_locals.events_since(0))
    # Each event appears exactly once; no dedup needed after first sync.
    assert [e.to_model for e in first] == ["gemma", "qwen", "gemma"]
    # Now coordinator A (c1) emits after B was already synced. With the
    # old single-offset logic the next sync would slice the flattened
    # [a0,a1,a2,b0] at offset 3 and re-emit b0 (duplicate). Per-coordinator
    # offsets must instead append only a2.
    # Use a fresh model key via a new router state: switch back to gemma.
    r1.ensure_resident("gemma")  # c1 now has 3 events
    assert composite_locals.event_count() == 4
    all_events = list(composite_locals.events_since(0))
    assert [e.to_model for e in all_events] == ["gemma", "qwen", "gemma", "gemma"]
    # events_since from previous checkpoint returns only the new local event.
    assert [e.to_model for e in composite_locals.events_since(3)] == ["gemma"]
    # Two remote coordinators: same per-coordinator correctness for remotes.
    b1 = _FakeBackend([_make_call_record("r-a1", usage={"input_tokens": 1})])
    b2 = _FakeBackend([_make_call_record("r-b1", usage={"input_tokens": 2})])
    rc1 = RemoteRuntimeCoordinator(b1)
    rc2 = RemoteRuntimeCoordinator(b2)
    composite_remotes = CompositeRuntimeCoordinator(
        local=None, remote=None, descriptor=_make_descriptor(kind="composite"),
        remotes=[rc1, rc2],
    )
    assert composite_remotes.event_count() == 2
    assert [e.label for e in composite_remotes.events_since(0)] == ["r-a1", "r-b1"]
    b1._records.append(_make_call_record("r-a2", usage={"input_tokens": 3}))
    assert composite_remotes.event_count() == 3
    assert [e.label for e in composite_remotes.events_since(0)] == ["r-a1", "r-b1", "r-a2"]
    assert [e.label for e in composite_remotes.events_since(2)] == ["r-a2"]
