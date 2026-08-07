"""Tests for the V4 monitor v2 (owner-approved spec).

Covers the monitor-v2 acceptance criteria
(``docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md``, section
"Дизайн обновлённого монитора"; ``docs/plans/V4_BOOK_RUN_MONITOR_TASK_RU.md``):

* usage-by-step-x-model grouping reuses ``phase_for_label()`` from
  ``v4_usage.py`` (never duplicated) and aggregates calls/tokens/cost;
* the cost column is hidden gracefully when a provider reports no cost
  (all ``reported_cost`` 0/None);
* multi-chapter discovery over ``--out-base`` (0/1/2 chapters, a chapter
  appears dynamically once its dir carries ``phase_progress.ndjson``);
* Step 8 shows an explicit "not started" wording before Step 8 begins
  (instead of ``formatting incidents=None ... terminal=None``);
* repair progress in the Step-7 phase line comes from ``region_done`` /
  ``region_*`` events (committed/debt), not from ``repair_report.json``
  presence alone;
* liveness/model activity is driven by the last ``usage.ndjson`` record
  and the last ``phase_progress.ndjson`` event (``server_logs`` are only
  "age since server start", never a liveness signal);
* the tracker stays read-only: neither ``render_report`` nor
  ``render_book_report`` (nor the CLI) writes into the monitored dirs.

Pure diagnostics — no subprocess, no HTTP, no ``llama-server``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pact_full_pipeline_runner_v1 import v4_phase_progress as tracker
from pact_full_pipeline_runner_v1 import v4_usage
from pact_v4.pipeline.phase_progress import PHASE_PROGRESS_FILENAME
from pact_v4.pipeline.usage_record import USAGE_FILENAME


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_ndjson(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _iso(seconds_ago: float) -> str:
    ts = datetime.now(timezone.utc).timestamp() - seconds_ago
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _usage_row(label: str, model: str = "deepseek-v4-flash",
               cost: float = 0.01, seconds_ago: float = 10.0,
               input_tokens: int = 100, output_tokens: int = 200,
               reasoning_tokens: int = 0, cached_input_tokens: int = 0) -> dict:
    return {
        "schema": "pact-v4-usage/ndjson/v1",
        "ts": _iso(seconds_ago),
        "label": label,
        "model_ref": f"opencode-go/{model}",
        "provider": "opencode-go",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cached_write_tokens": 0,
        "reported_cost": cost,
    }


def _chapter_dir(tmp_path: Path, name: str, started_at: str) -> Path:
    out = tmp_path / name
    _write(out / "chunk_plan.json", {"chunks": [{"chunk_id": "c1"}, {"chunk_id": "c2"}]})
    _write_ndjson(out / "journal.ndjson", [
        {"chunk_id": "c1", "outcome": "selected"},
        {"chunk_id": "c2", "outcome": "selected"},
    ])
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started",
         "ts": started_at, "started_at": started_at, "resumed_from_index": 0},
    ])
    return out


# ---------------------------------------------------------------------------
# Usage by step x model (reuses phase_for_label)
# ---------------------------------------------------------------------------


def test_usage_grouping_reuses_phase_for_label(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", cost=0.01),
        _usage_row("phase2b/fidelity_first/chunk0002", cost=0.02),
        _usage_row("phase2c/qwen_fidelity", model="qwen3.7-plus", cost=0.03),
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", cost=0.04),
        _usage_row("phase3/gemma_russian_review", cost=0.05),
        _usage_row("phase4/region_repair", cost=0.06),
        _usage_row("phase4/region_fidelity_gate", model="qwen3.7-plus", cost=0.07),
        _usage_row("phase5/formatting_align", cost=0.08),
    ])

    groups = tracker._usage_group_rows(tracker._read_usage_rows(out))
    by_group = {(g["label_group"], g["model"]): g for g in groups}

    # phase2b collapses per-chunk labels into one "generation" group.
    assert by_group[("phase2b generation", "deepseek-v4-flash")]["calls"] == 2
    assert by_group[("phase2b generation", "deepseek-v4-flash")]["step"] == "Steps1-5"
    # phase2c/phase4 keep their label sub-role.
    assert by_group[("phase2c qwen_fidelity", "qwen3.7-plus")]["step"] == "Step2c"
    assert by_group[("phase4 region_repair", "deepseek-v4-flash")]["step"] == "Step7"
    assert by_group[("phase4 region_fidelity_gate", "qwen3.7-plus")]["step"] == "Step7"
    assert by_group[("phase3 audit", "qwen3.7-plus")]["step"] == "Step6"
    assert by_group[("phase5 formatting", "deepseek-v4-flash")]["step"] == "Step8"

    # The step mapping goes through phase_for_label — prove the reuse by
    # checking the label->phase leg is literally v4_usage's function.
    assert tracker.phase_for_label is v4_usage.phase_for_label
    assert by_group[("phase2b generation", "deepseek-v4-flash")]["reported_cost"] == 0.03
    assert by_group[("phase3 audit", "qwen3.7-plus")]["reported_cost"] == 0.04


def test_usage_grouping_canonicalizes_legacy_hyphen_labels(tmp_path: Path):
    # Non-blocking review observation: legacy adapter labels
    # ("phase2c-qwen-fidelity") carry the phase namespace inside the token;
    # the label-group must render the canonical "phase2c qwen_fidelity",
    # not the raw "phase2c-qwen-fidelity qwen_fidelity".
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2c-qwen-fidelity", model="qwen3.7-plus", cost=0.01),
        _usage_row("phase2c-gemma-russian-preference", cost=0.02),
    ])

    groups = tracker._usage_group_rows(tracker._read_usage_rows(out))
    by_group = {(g["label_group"], g["model"]): g for g in groups}

    assert by_group[("phase2c qwen_fidelity", "qwen3.7-plus")]["step"] == "Step2c"
    assert by_group[("phase2c gemma_preference", "deepseek-v4-flash")]["step"] == "Step2c"
    assert not any(g.startswith("phase2c-qwen-fidelity") for g, _ in by_group)


def test_usage_block_hides_cost_column_when_all_zero(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Zero-cost provider: reported_cost absent / 0 on every row.
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", cost=0.0),
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", cost=None),
    ])
    report = tracker.render_report(out)
    block = report.split("-- usage by step x model", 1)[1]
    assert "cost" not in block
    assert "calls" in block and "input" in block

    # With real cost the column reappears.
    _write_ndjson(out / USAGE_FILENAME, [_usage_row("phase2b/balanced_literary/chunk0001", cost=0.01)])
    report2 = tracker.render_report(out)
    block2 = report2.split("-- usage by step x model", 1)[1]
    assert "cost" in block2 and "$0.01" in block2


def test_usage_block_reasoning_cached_columns_only_when_nonzero(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", reasoning_tokens=0, cached_input_tokens=0),
    ])
    block = tracker.render_report(out).split("-- usage by step x model", 1)[1]
    assert "reasoning" not in block and "cached" not in block

    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", reasoning_tokens=5, cached_input_tokens=7),
    ])
    block2 = tracker.render_report(out).split("-- usage by step x model", 1)[1]
    assert "reasoning" in block2 and "cached" in block2


# ---------------------------------------------------------------------------
# Multi-chapter discovery (--out-base)
# ---------------------------------------------------------------------------


def test_multi_chapter_discovery_dynamic(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()

    # 0 chapters -> no chapters table.
    report0 = tracker.render_book_report(base)
    assert "no chapter_*" in report0

    # 1 chapter appears.
    _chapter_dir(base, "chapter_0001", _iso(3600))
    report1 = tracker.render_book_report(base)
    assert "-- chapters (1)" in report1
    assert "chapter_0001" in report1

    # A second chapter appears dynamically on re-render.
    _chapter_dir(base, "chapter_0002", _iso(10))
    report2 = tracker.render_book_report(base)
    assert "-- chapters (2)" in report2
    assert "chapter_0002" in report2


def test_chapters_table_rows_and_total(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    ch1 = _chapter_dir(base, "chapter_0001", _iso(3600))
    _write_ndjson(ch1 / USAGE_FILENAME, [_usage_row("phase2b/balanced_literary/chunk0001", cost=1.0)])
    ch2 = _chapter_dir(base, "chapter_0002", _iso(10))
    _write_ndjson(ch2 / USAGE_FILENAME, [_usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", cost=2.0)])

    report = tracker.render_book_report(base)
    assert "chunks" in report and "16/16" not in report  # 2/2 in this fixture
    assert "2/2" in report
    assert "TOTAL" in report
    # calls 1+1=2, cost 1.00+2.00=3.00
    assert "$3.00" in report


def test_chapters_table_hides_cost_when_none_reported(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    ch1 = _chapter_dir(base, "chapter_0001", _iso(3600))
    _write_ndjson(ch1 / USAGE_FILENAME, [_usage_row("phase2b/balanced_literary/chunk0001", cost=None)])
    report = tracker.render_book_report(base)
    table = report.split("-- active chapter", 1)[0]
    assert "cost(prov.)" not in table


def test_chapters_table_hides_cost_when_all_zero_reported(tmp_path: Path):
    # Regression for review MEDIUM: a chapter whose usage rows all carry
    # reported_cost 0.0 (or a 0.0/None mix) must hide the cost column —
    # "all 0/None -> hide gracefully", never "$0.00" noise.
    base = tmp_path / "book"
    base.mkdir()
    ch1 = _chapter_dir(base, "chapter_0001", _iso(3600))
    _write_ndjson(ch1 / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", cost=0.0),
        _usage_row("phase2c/qwen_fidelity", model="qwen3.7-plus", cost=None),
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", cost=0.0),
    ])
    report = tracker.render_book_report(base)
    table = report.split("-- active chapter", 1)[0]
    assert "cost(prov.)" not in table
    assert "$0.00" not in table
    # Calls are still counted; the cost column is the only casualty.
    assert "3" in table

    # A single non-zero reported_cost anywhere brings the column back.
    _write_ndjson(ch1 / USAGE_FILENAME, [_usage_row("phase2b/balanced_literary/chunk0001", cost=0.01)])
    report2 = tracker.render_book_report(base)
    table2 = report2.split("-- active chapter", 1)[0]
    assert "cost(prov.)" in table2
    assert "$0.01" in table2


# ---------------------------------------------------------------------------
# Step 8 not-started wording
# ---------------------------------------------------------------------------


def _step7_run_dir(tmp_path: Path) -> Path:
    out = tmp_path / "run_step7"
    _write(out / "chunk_plan.json", {"chunks": [{"chunk_id": "c1"}, {"chunk_id": "c2"}]})
    _write_ndjson(out / "journal.ndjson", [
        {"chunk_id": "c1", "outcome": "selected"},
        {"chunk_id": "c2", "outcome": "selected"},
    ])
    _write(out / "b2_handoff.json", {"chunks": [
        {"chunk_id": "c1", "audit_status": "clean"},
        {"chunk_id": "c2", "audit_status": "findings_present"},
    ]})
    return out


def test_step8_not_started_wording(tmp_path: Path):
    out = _step7_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started",
         "ts": _iso(30), "started_at": _iso(30)},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "repair_round_started",
         "ts": _iso(20), "round_number": 1},
    ])
    report = tracker.render_report(out)
    assert "Step 8: not started (ожидание formatting/terminal)" in report
    assert "formatting incidents=None" not in report


# ---------------------------------------------------------------------------
# Repair progress from region events
# ---------------------------------------------------------------------------


def test_step7_phase_line_shows_region_progress(tmp_path: Path):
    out = _step7_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started",
         "ts": _iso(60), "started_at": _iso(60)},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "repair_round_started",
         "ts": _iso(50), "round_number": 2},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "region_started",
         "ts": _iso(40), "chunk_id": "c2", "repair_id": "r1", "target_pids": ["p1"]},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "region_done",
         "ts": _iso(30), "chunk_id": "c2", "repair_id": "r1", "target_pids": ["p1"],
         "committed": True, "reason": "ok"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "region_done",
         "ts": _iso(20), "chunk_id": "c2", "repair_id": "r2", "target_pids": ["p2"],
         "committed": False, "reason": "no_candidate"},
    ])
    events = tracker._load_events(out)
    phase, basis = tracker._detect_phase(out, events)
    assert phase == "step7"
    # repair_report.json absent is NOT "repair not started": the basis must
    # surface committed/debt from the region events (owner eff-a1a2).
    assert "repair_report.json absent" in basis
    assert "committed=1" in basis and "debt=1" in basis
    assert "round 2" in basis


# ---------------------------------------------------------------------------
# Liveness / model activity from usage.ndjson
# ---------------------------------------------------------------------------


def test_liveness_uses_usage_ndjson(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Fresh usage record -> alive even though server_logs are old (the
    # eff-a1a2 remote-run case: opencode_serve_*.log static since server
    # start, usage.ndjson written per call).
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", seconds_ago=10),
    ])
    events = tracker._load_events(out)
    identity = tracker._identity(out, events)
    assert identity["alive"] is True
    assert "usage.ndjson" in identity["alive_basis"]

    # Stale usage + stale events -> not alive; server_logs age is shown
    # separately and must NOT drive the verdict.
    stale = _chapter_dir(tmp_path, "chapter_0002", _iso(7200))
    _write_ndjson(stale / USAGE_FILENAME, [
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", seconds_ago=7200),
    ])
    events2 = tracker._load_events(stale)
    identity2 = tracker._identity(stale, events2)
    assert identity2["alive"] is False


def test_model_activity_shows_last_usage_record(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", seconds_ago=5),
    ])
    report = tracker.render_report(out)
    assert "last usage.ndjson:" in report
    assert "label=phase3/qwen_chapter_audit" in report
    assert "model=opencode-go/qwen3.7-plus" in report
    assert "age since server start" in report


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def _snapshot(directory: Path) -> dict:
    return {
        p.relative_to(directory).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(directory.rglob("*")) if p.is_file()
    }


def test_render_book_report_is_read_only(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    ch1 = _chapter_dir(base, "chapter_0001", _iso(3600))
    _write_ndjson(ch1 / USAGE_FILENAME, [_usage_row("phase2b/balanced_literary/chunk0001")])
    ch2 = _chapter_dir(base, "chapter_0002", _iso(10))
    _write_ndjson(ch2 / USAGE_FILENAME, [_usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus")])

    before = _snapshot(base)
    tracker.render_book_report(base)
    after = _snapshot(base)
    assert after == before


def test_cli_requires_exactly_one_target(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # No target -> argparse error (exit code 2).
    try:
        tracker.main([])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit for missing --out-dir/--out-base")
    # Exactly one target works and stays read-only.
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in out.iterdir()}
    rc = tracker.main(["--out-dir", str(out)])
    assert rc == 0
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in out.iterdir()}
    assert after == before
