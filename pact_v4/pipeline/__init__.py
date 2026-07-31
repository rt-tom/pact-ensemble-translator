"""Pact v4 driver-level orchestration.

This package wires the Phase 1 / Phase 2 library modules into a single
end-to-end run for one chapter. The library modules
(``pact_v4.phase1.*``, ``pact_v4.phase2.*``) are pure and free of
network/production wiring; the runtime adapters in ``pact_v4.runtime``
provide the real ``ModelCaller``/``QwenEvaluator``/``GemmaSelector``
implementations. This package's only job is to *glue* the two together
and emit the run's provenance artefacts.

Phase 3–6 (audit, repair, formatting) are out of scope: the driver
exits with the cascaded selection per chunk. The future comparison tool
is a separate, post-driver consumer of the artefacts written here.
"""
from __future__ import annotations

from pact_v4.pipeline.v4_phase12_draft_runner import (
    ChapterRunResult,
    PipelineConfig,
    run_chapter,
)

__all__ = ["ChapterRunResult", "PipelineConfig", "run_chapter"]
