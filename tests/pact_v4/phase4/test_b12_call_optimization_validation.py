"""B12 call-optimization validation on the chapter 0001 run_004_remote artifacts.

Recomputes the model-call budget with the B12 optimizations on the frozen
run artifacts (``D:\\pact\\gate_bench_runs\\v4_phase12_strict_0001\\
run_004_remote``):

  * formatting model-fallback: per-PID (78) -> per-chunk batches (15);
  * repair re-gate: per-region (95) -> per-chunk batches (16);

and asserts the full cycle (with complete formatting) fits the 500-request
budget. A light smoke runs ``run_formatting_align`` over the real chapter
source + run_004 translations with a batch-capable fake caller and asserts
the batched path resolves the same spans/incidents as the per-PID path with
fewer calls (parity contract).

The test is skipped when the run artifacts are not present (they live on a
development machine, not in the repository).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase5 import formatting as fmt

RUN_DIR = Path(r"D:\pact\gate_bench_runs\v4_phase12_strict_0001\run_004_remote")
CHAPTER_HTML = Path(r"D:\pact\pact_chapters\0001_bonds-1-1.html")

pytestmark = pytest.mark.skipif(
    not RUN_DIR.exists() or not CHAPTER_HTML.exists(),
    reason="chapter 0001 run artifacts are not present on this machine",
)


def _load(name: str):
    return json.loads((RUN_DIR / name).read_text(encoding="utf-8"))


def test_b12_formatting_batches_pids_per_chunk():
    chunk_plan = _load("chunk_plan.json")
    pid_to_chunk: dict[str, str] = {}
    for chunk in chunk_plan["chunks"]:
        for pid in chunk["pids"]:
            pid_to_chunk[pid] = chunk["chunk_id"]

    outcome = _load("formatting_report.json")["outcome"]
    unresolved_pids = sorted({inc["pid"] for inc in outcome["incidents"]})
    by_chunk = Counter(pid_to_chunk.get(pid, "?") for pid in unresolved_pids)
    # Per-chunk batching: one call per chunk that has unresolved PIDs.
    assert len(by_chunk) < len(unresolved_pids)
    assert len(unresolved_pids) - len(by_chunk) > 0


def test_b12_re_gate_batches_regions_per_chunk():
    cache = _load("repair_cache.json")
    units = cache["cache"]["units"]
    items = list(units.values()) if isinstance(units, dict) else units
    regates = [
        u["record"] for u in items
        if len(u["record"].get("gate_trace") or []) >= 2
    ]
    by_chunk = Counter(r["chunk_id"] for r in regates)
    assert len(by_chunk) < len(regates)
    assert len(regates) - len(by_chunk) > 0


def test_b12_full_cycle_fits_500_budget():
    record = _load("strict_chapter_trial_record.json")
    total = record["runtime"]["remote_calls"]["count"]
    chunk_plan = _load("chunk_plan.json")
    pid_to_chunk: dict[str, str] = {}
    for chunk in chunk_plan["chunks"]:
        for pid in chunk["pids"]:
            pid_to_chunk[pid] = chunk["chunk_id"]

    outcome = _load("formatting_report.json")["outcome"]
    unresolved_pids = sorted({inc["pid"] for inc in outcome["incidents"]})
    fmt_batches = len({pid_to_chunk.get(pid, "?") for pid in unresolved_pids})

    cache = _load("repair_cache.json")
    units = cache["cache"]["units"]
    items = list(units.values()) if isinstance(units, dict) else units
    regates = [
        u["record"] for u in items
        if len(u["record"].get("gate_trace") or []) >= 2
    ]
    gate_batches = len({r["chunk_id"] for r in regates})

    # Baseline: run_004 used the whole budget without formatting (0 formatting
    # calls, budget exhausted before the step). Full cycle per-PID would add
    # one call per unresolved PID. B12 batches both formatting and re-gates
    # per chunk, so the full cycle must fit the 500 budget.
    baseline_full = total + len(unresolved_pids)
    savings = (len(unresolved_pids) - fmt_batches) + (len(regates) - gate_batches)
    new_total = baseline_full - savings
    assert new_total <= 500, (
        f"full cycle with batching must fit the budget: {new_total} > 500"
    )


class _EmptyBatchCaller:
    """Batch-capable formatting caller that returns no mappings (parity probe)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *, pid, source_text, translation, spans) -> str:
        self.calls += 1
        return json.dumps({"mappings": []}, ensure_ascii=False)

    def batch(self, items) -> str:
        self.calls += 1
        return json.dumps({"mappings": []}, ensure_ascii=False)


def test_b12_formatting_batched_parity_and_fewer_calls():
    # Light smoke on the real chapter: the batched path resolves exactly the
    # same spans/incidents as the per-PID path (parity) with fewer calls.
    chunk_plan = _load("chunk_plan.json")
    blocks = parse_source_html(CHAPTER_HTML.read_text(encoding="utf-8"))
    translations = _load("translations.json")

    per_pid_caller = _EmptyBatchCaller()
    per_pid = fmt.run_formatting_align(
        blocks=blocks, translation=translations,
        formatting_caller=per_pid_caller, backend_identity_hash="x" * 32,
    )
    batched_caller = _EmptyBatchCaller()
    batched = fmt.run_formatting_align(
        blocks=blocks, translation=translations,
        formatting_caller=batched_caller, backend_identity_hash="x" * 32,
        pid_batches=[tuple(chunk["pids"]) for chunk in chunk_plan["chunks"]],
    )
    assert per_pid.model_fallback_count == batched.model_fallback_count
    assert batched.model_call_count < per_pid.model_call_count
    assert batched.incidents == per_pid.incidents
    assert batched.formatted_text == per_pid.formatted_text
