"""Tests for ``ModelRouter``'s swap-on-demand lease reuse.

``ModelRouter.ensure_resident`` is the piece of
``pact_v4.pipeline.v4_phase12_strict_runner`` that makes ``Gpref(N)`` and
``Ggen(N+1)`` share one Gemma lease instead of always restarting (see
``docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md``, "Подсчёт
перезапусков"). These tests exercise it against a fake in-process
``LifecycleAdapter`` -- no subprocess, no HTTP, no real ``llama-server``.
"""
from __future__ import annotations

from typing import List, Tuple

from pact_v4.runtime.model_lifecycle import ModelRouter


class FakeLifecycleAdapter:
    """Records start/stop calls with deterministic fake timings."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self._next_pid = 1000

    def start(self, model_key: str, profile: str, extra_args: list, retries: int = 1):
        self.calls.append(("start", model_key))
        self._next_pid += 1
        return 1.5, 0  # cold_acquire_seconds, retries_used

    def stop(self):
        self.calls.append(("stop", ""))
        return 0.5, True, 0  # unload_seconds, released, final_bytes

    def sample_vram(self) -> int:
        return 1024 * 1024 * 100


def _make_router() -> Tuple[ModelRouter, FakeLifecycleAdapter]:
    adapter = FakeLifecycleAdapter()
    router = ModelRouter(
        adapter,
        role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
        role_args={"gemma": ["--gemma-flag"], "qwen": ["--qwen-flag"]},
    )
    return router, adapter


def test_first_ensure_resident_is_a_startup_not_a_restart():
    router, adapter = _make_router()
    record = router.ensure_resident("gemma")
    assert record is not None
    assert record.from_model is None
    assert record.to_model == "gemma"
    assert router.current_model == "gemma"
    assert adapter.calls == [("start", "gemma")]  # no stop() before the first start


def test_repeated_ensure_resident_same_model_is_a_noop():
    router, adapter = _make_router()
    router.ensure_resident("gemma")
    second = router.ensure_resident("gemma")
    assert second is None
    assert adapter.calls == [("start", "gemma")]  # exactly one start, no restart


def test_switching_model_stops_then_starts():
    router, adapter = _make_router()
    router.ensure_resident("gemma")
    record = router.ensure_resident("qwen")
    assert record is not None
    assert record.from_model == "gemma"
    assert record.to_model == "qwen"
    assert adapter.calls == [("start", "gemma"), ("stop", ""), ("start", "qwen")]


def test_gpref_and_next_ggen_share_one_lease():
    """Gpref(N) then Ggen(N+1) on the same lease -> exactly one restart
    for the whole Q -> G handoff, not two."""
    router, adapter = _make_router()
    router.ensure_resident("gemma")  # Ggen(1) -- startup
    router.ensure_resident("qwen")  # Q(1) -- restart 1
    pref_record = router.ensure_resident("gemma")  # Gpref(1) -- restart 2
    gen2_record = router.ensure_resident("gemma")  # Ggen(2) -- same lease, no restart
    assert pref_record is not None
    assert gen2_record is None
    assert len(router.switches) == 3  # startup + 2 restarts, not 3 restarts
    assert [sw.to_model for sw in router.switches] == ["gemma", "qwen", "gemma"]


def test_strict_ten_chunk_sequence_matches_architecture_doc_restart_count():
    """Reproduces the exact sequence from the architecture doc's "Подсчёт
    перезапусков" table for the strict variant with 10 high-risk chunks,
    every one needing a Gemma preference call: 21 startups, 20 restarts."""
    router, _adapter = _make_router()
    router.ensure_resident("gemma")  # Ggen1 (startup)
    for _ in range(10):
        router.ensure_resident("qwen")  # Q(N)
        router.ensure_resident("gemma")  # Gpref(N) [/ Ggen(N+1) next loop, same lease]
    assert len(router.switches) == 21
    assert (len(router.switches) - 1) == 20


def test_release_stops_without_recording_a_switch():
    router, adapter = _make_router()
    router.ensure_resident("gemma")
    unload_seconds = router.release()
    assert unload_seconds == 0.5
    assert router.current_model is None
    assert len(router.switches) == 1  # release() is not itself a SwitchRecord
    assert adapter.calls[-1] == ("stop", "")


def test_release_when_nothing_resident_is_a_noop():
    router, adapter = _make_router()
    assert router.release() is None
    assert adapter.calls == []
