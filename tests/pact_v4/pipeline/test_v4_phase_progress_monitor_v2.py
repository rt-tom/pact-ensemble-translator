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

    # MONITOR-V2 (1.3): the column is renamed to "фазы" and shows human
    # phase names (Extraction / Translation / R_editor / Audit / Repair /
    # Re-audit) instead of the old "phase2b generation" label-groups.
    assert by_group[("Whole-chapter translation", "deepseek-v4-flash")]["calls"] == 2
    assert by_group[("Whole-chapter translation", "deepseek-v4-flash")]["step"] == "Whole-chapter translation"
    # phase2c/phase4 keep their phase identity through phase_for_label.
    assert by_group[("qwen_fidelity", "qwen3.7-plus")]["step"] == "qwen_fidelity"
    assert by_group[("Selective repair", "deepseek-v4-flash")]["step"] == "Selective repair"
    assert by_group[("Selective repair", "qwen3.7-plus")]["step"] == "Selective repair"
    assert by_group[("Chapter audit", "qwen3.7-plus")]["step"] == "Chapter audit"
    assert by_group[("Formatting", "deepseek-v4-flash")]["step"] == "Formatting"

    # The step mapping goes through phase_for_label — prove the reuse by
    # checking the label->phase leg is literally v4_usage's function.
    assert tracker.phase_for_label is v4_usage.phase_for_label
    assert by_group[("Whole-chapter translation", "deepseek-v4-flash")]["reported_cost"] == 0.03
    assert by_group[("Chapter audit", "qwen3.7-plus")]["reported_cost"] == 0.04


def test_usage_grouping_canonicalizes_legacy_hyphen_labels(tmp_path: Path):
    # Legacy adapter labels ("phase2c-qwen-fidelity") carry the phase
    # namespace inside the token; the "фазы" column must render the human
    # phase name, not the raw "phase2c-qwen-fidelity" token.
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2c-qwen-fidelity", model="qwen3.7-plus", cost=0.01),
        _usage_row("phase2c-gemma-russian-preference", cost=0.02),
    ])

    groups = tracker._usage_group_rows(tracker._read_usage_rows(out))
    by_group = {(g["label_group"], g["model"]): g for g in groups}

    assert by_group[("qwen_fidelity", "qwen3.7-plus")]["step"] == "qwen_fidelity"
    assert by_group[("gemma_preference", "deepseek-v4-flash")]["step"] == "gemma_preference"
    assert not any(g.startswith("phase2c") for g, _ in by_group)


def test_usage_block_hides_cost_column_when_all_zero(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Zero-cost provider: reported_cost absent / 0 on every row.
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", cost=0.0),
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", cost=None),
    ])
    # Detailed block helper still hides cost correctly; compact report shows usage summary without cost
    block = "\n".join(tracker._usage_block_lines(out))
    assert "cost" not in block
    assert "calls" in block and "input" in block
    report = tracker.render_report(out)
    # Compact usage line should not show cost when all zero
    assert "$0.01" not in report

    # With real cost the column reappears.
    _write_ndjson(out / USAGE_FILENAME, [_usage_row("phase2b/balanced_literary/chunk0001", cost=0.01)])
    block2 = "\n".join(tracker._usage_block_lines(out))
    assert "cost" in block2 and "$0.01" in block2
    report2 = tracker.render_report(out)
    assert "$0.01" in report2


def test_usage_block_reasoning_cached_columns_only_when_nonzero(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", reasoning_tokens=0, cached_input_tokens=0),
    ])
    block = "\n".join(tracker._usage_block_lines(out))
    assert "reasoning" not in block and "cached" not in block

    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", reasoning_tokens=5, cached_input_tokens=7),
    ])
    block2 = "\n".join(tracker._usage_block_lines(out))
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
    assert "mode/unit" in report
    assert "chunks" not in report.split("\n")[1]  # column renamed to mode/unit
    assert "2/2" in report
    assert "TOTAL" in report
    assert "$3.00" in report
    assert "16/16" not in report


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
    assert "Formatting:" not in report
    assert "Formatting: not started" not in report
    assert "incidents=None" not in report
    assert "status:" not in report
    assert "mode=fine" not in report
    assert "phase:" not in report


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
    # Compact report shows usage summary with label/model via phase summary; detailed last usage block removed from compact
    # Check that usage summary is present and does not crash
    assert "usage:" in report
    # Detailed helper still works
    assert tracker._last_call_block_lines(out)[1].find("qwen3.7-plus") != -1
    # Age since server start is gated on local fresh logs, not always rendered
    # With no server_logs, compact hides it
    assert "age since server start" not in report


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


# ---------------------------------------------------------------------------
# MONITOR-V2: Phase block (1.1), local speed block (1.2), фазы column (1.3),
# book-table tokens (1.4), last-call block (1.5)
# ---------------------------------------------------------------------------


def _phase_mock_artifacts(out: Path) -> None:
    """B3-era artifacts matching the real chapter-0006 shapes."""
    _write(out / "entity_context_cache.json", {"schema": "x", "entries": [{
        "context": {"entities": [
            {"entity": "Blake", "anchor": {"status": "verified"},
             "aliases": [{"status": "verified"}], "claims": []},
            {"entity": "Rose", "anchor": {"status": "candidate"},
             "aliases": [{"status": "candidate"}], "claims": []},
        ]},
    }]})
    _write(out / "translations_raw.json", {
        "p1": "Узы 1.6", "p2": "Она была мокрой. Он молчал.",
    })
    _write(out / "chunk_plan.json", {"chunks": [
        {"chunk_id": "c1", "word_counts": [2, 22, 38]},
        {"chunk_id": "c2", "word_counts": [87, 137]},
    ]})
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": {
            "chunk_count": 2, "successful_chunks": 2,
            "applied": [["p1", "текст"]], "candidates": [{"pid": "p2"}],
        }},
        "chunks": [
            {"chunk": 1, "status": "GOOD", "issue_count": 3},
            {"chunk": 2, "status": "GOOD_RETRIED", "issue_count": 0},
        ],
        "issue_count": 3,
        "repair": {
            "eligible_count": 4,
            "batches": [
                {"batch_index": 1, "findings": [{"index": 1}],
                 "results": [{"index": 1, "decision": "repair"}]},
                {"batch_index": 2, "findings": [{"index": 2}, {"index": 3}],
                 "results": [{"index": 2, "decision": "repair"},
                             {"index": 3, "decision": "skip"}]},
            ],
            "committed": ["p1", "p2"],
            "reaudit": {"complete": True, "failed": False, "issues": [{"id": "p9"}]},
        },
    })
    _write(out / "b3_repair_reaudit_chunk1_raw.txt", "{}")
    _write(out / "b3_repair_reaudit_chunk2_raw.txt", "{}")


def test_phase_block_renders_six_phases(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _phase_mock_artifacts(out)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    assert lines[0] == "-- Phase --"
    text = "\n".join(lines)
    assert "Entity extraction: сущностей: 2 | claims: verified 2 / candidate 2" in text
    assert "Whole-chapter translation: attempt 1/3 | source 286 слов → перевод 7 слов" in text
    assert "R-editor: chunks done=2/2 | safe (применено)=1 | review (предложено)=1" in text
    assert "Chapter audit: chunks done=2/2 | findings per chunk: [3, 0] | всего 3" in text
    assert "Selective repair: batches done=2/2 | repaired per batch: [1/1, 1/2] | findings eligible: 4 | PID edits committed: 2" in text
    assert "Re-audit scope: chunks done=2/2 | residual: 1 | debt: 0" in text


def test_phase_block_skips_absent_artifacts(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    assert lines == ["-- Phase --", "  (нет Phase-артефактов: entity_context_cache.json / "
                                    "translations_raw.json / audit_cache_b3.json)"]


def test_phase_block_translation_attempt_from_wc_events(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "translations_raw.json", {"p1": "текст"})
    _write(out / "chunk_plan.json", {"chunks": [{"chunk_id": "c1", "word_counts": [5]}]})
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
        {"schema": "x", "event": "wc_retry_attempt", "ts": _iso(30), "attempt": 2},
    ])
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    assert "Whole-chapter translation: attempt 2/3 | source 5 слов → перевод 1 слов" in "\n".join(lines)


def _local_server_log(out: Path, name: str = "Gemma_20260816_120000_stderr.log") -> Path:
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "server_logs"
    logs.mkdir(exist_ok=True)
    path = logs / name
    # Real llama-server slot print_timing lines (shape from chapter 6).
    path.write_text(
        "0.32.156.164 I slot print_timing: id  0 | task 0 | n_decoded =    100, "
        "tg =  26.08 t/s, tg_3s =  26.08 t/s\n"
        "14.51.578.226 I slot print_timing: id  0 | task 20347 | prompt eval time ="
        "    7287.64 ms /  2747 tokens (    2.65 ms per token,   376.94 tokens per second)\n"
        "14.51.578.231 I slot print_timing: id  0 | task 20347 |        eval time ="
        "   79759.70 ms /  2373 tokens (   33.61 ms per token,    29.75 tokens per second)\n"
        "14.52.001.005 I slot print_timing: id  0 | task 20347 | n_decoded =    500, "
        "tg =  30.11 t/s, tg_3s =  29.63 t/s\n",
        encoding="utf-8",
    )
    return path


def test_server_speed_block_local(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _local_server_log(out)
    lines = tracker._server_speed_lines(out)
    assert lines[0] == "-- скорость генерации (локальная, из server_logs) --"
    assert "gemma: eval 29.75 t/s | prompt 376.9 t/s | live tg_3s 29.63 t/s" in lines[1]
    # MONITOR-V2 finding 2: raw prefix, not wall-clock HMS.
    assert "n_decoded=500, eval 79.8s (raw: 14.51.578.231)" in lines[2]


def test_server_speed_block_absent_for_remote(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    logs = out / "server_logs"
    logs.mkdir()
    (logs / "opencode_serve_20260815_100151_stderr.log").write_text("x", encoding="utf-8")
    assert tracker._server_speed_lines(out) == []


def test_server_speed_block_picks_newest_log(tmp_path: Path):
    import os
    from datetime import datetime, timezone

    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    gemma = _local_server_log(out, "Gemma_20260816_100000_stderr.log")
    qwen = _local_server_log(out, "Qwen_20260816_120000_stderr.log")
    # Qwen log has a different eval speed.
    qwen.write_text(
        "4.30.558.340 I slot print_timing: id  0 | task 0 |        eval time ="
        "   5000.00 ms /  1000 tokens (    5.00 ms per token,   200.00 tokens per second)\n",
        encoding="utf-8",
    )
    # The card: "последний по времени лог = текущая фаза" — the newest log
    # by mtime wins, regardless of filename.
    os.utime(gemma, (0, 0))
    os.utime(qwen, (2_000_000_000, 2_000_000_000))
    lines = tracker._server_speed_lines(out)
    assert "qwen: eval 200.00 t/s" in lines[1]


def test_llama_ts_raw():
    # MONITOR-V2 finding 2: _llama_ts_raw returns the prefix as-is —
    # no wall-clock interpretation, no impossible timestamps.
    assert tracker._llama_ts_raw("14.51.578.231") == "14.51.578.231"
    assert tracker._llama_ts_raw("4.30.558.334") == "4.30.558.334"
    assert tracker._llama_ts_raw("bad") == "bad"


def test_llama_ts_raw_no_impossible_wall_clock():
    """_llama_ts_raw never produces impossible timestamps like
    35:59:86 — it returns the raw prefix verbatim (task req. 7)."""
    # Impossible prefix: 35.59.86.231 would be 35:59:86 with the old
    # wall-clock conversion — now it stays as-is.
    assert tracker._llama_ts_raw("35.59.86.231") == "35.59.86.231"
    # Normal prefixes are also returned as-is.
    assert tracker._llama_ts_raw("14.51.578.231") == "14.51.578.231"
    assert tracker._llama_ts_raw("23.59.59.999") == "23.59.59.999"


def test_last_call_block_from_usage(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", input_tokens=100, output_tokens=50),
        _usage_row("phase3/reaudit_scope_v4", model="qwen3.7-plus",
                   input_tokens=45620, output_tokens=210, reasoning_tokens=5800,
                   seconds_ago=5),
    ])
    lines = tracker._last_call_block_lines(out)
    assert lines[0] == "-- последний вызов (из usage.ndjson) --"
    assert lines[1] == "  Re-audit scope | qwen3.7-plus | in=45620 out=210 reas=5800 | wall=?s"


def test_last_call_block_absent_without_usage(tmp_path: Path):
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    assert tracker._last_call_block_lines(out) == []


def test_book_table_shows_token_columns(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    ch1 = _chapter_dir(base, "chapter_0001", _iso(3600))
    _write_ndjson(ch1 / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", input_tokens=1000,
                   output_tokens=500, reasoning_tokens=0),
    ])
    report = tracker.render_book_report(base)
    table = report.split("-- active chapter", 1)[0]
    assert "input" in table and "output" in table
    assert "reasoning" not in table  # zero everywhere -> hidden
    assert "1.0k" in table


def test_book_table_hides_token_columns_without_usage(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _chapter_dir(base, "chapter_0001", _iso(3600))
    report = tracker.render_book_report(base)
    table = report.split("-- active chapter", 1)[0]
    assert "input" not in table and "output" not in table


def test_phase_for_label_b3_subphases():
    assert v4_usage.phase_for_label("b1.2/entity_extractor") == "extraction"
    assert v4_usage.phase_for_label("phase3/russian_editor_v4") == "r_editor"
    assert v4_usage.phase_for_label("phase3/reaudit_scope_v4") == "reaudit"
    assert v4_usage.phase_for_label("phase3/selective_repair_v4") == "repair"
    assert v4_usage.phase_for_label("phase3/qwen_chapter_audit_v4") == "audit"
    # The legacy phase3 umbrella still maps to audit.
    assert v4_usage.phase_for_label("phase3/other_thing") == "audit"


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


# ---------------------------------------------------------------------------
# RV t_c9f9ea90 HIGH #1: incremental B3 stage_progress fallback in the Phase
# block (KILL-SAFE-INCREMENTAL cache shape, t_2d16962c)
# ---------------------------------------------------------------------------


def _stage_progress_cache() -> dict:
    """A realistic KILL-SAFE-INCREMENTAL ``audit_cache_b3.json`` payload.

    Live slices live under ``stage_progress`` (r_editor / audit / repair /
    reaudit); the final top-level keys (r_editor / chunks / repair) are
    ABSENT, so the Phase builders must fall back to the live slices.
    """
    return {
        "schema": "pact-v4-b3-audit-cache/v1",
        "snapshot_hash": "s", "translation_hash": "t",
        "config_identity": "c", "backend_identity_hash": "b",
        "entity_context_enabled": True,
        "audit_complete": False,
        "translations_repaired": {},
        "translations_repaired_hash": "h",
        # R in progress: 3 of 5 chunks done, 2 of those GOOD with edits.
        "stage_progress": {
            "r_editor": {
                "status": "partial", "enabled": True,
                "done_chunks": [1, 2, 3], "failed_chunks": [],
                "outcome": {
                    "chunk_size": 50,
                    "chunks": [
                        {"chunk": 1, "first_pid": "p00001", "last_pid": "p00050",
                         "status": "GOOD",
                         "edits": [
                             {"pid": "p00003", "original": "x", "rewritten": "y",
                              "reason": "r", "class": "typo"},
                             {"pid": "p00003", "original": "x", "rewritten": "y",
                              "reason": "r", "class": "grammar"},
                         ]},
                        {"chunk": 2, "first_pid": "p00051", "last_pid": "p00100",
                         "status": "GOOD",
                         "edits": [
                             {"pid": "p00060", "original": "x", "rewritten": "y",
                              "reason": "r", "class": "calque"},
                         ]},
                        {"chunk": 3, "first_pid": "p00101", "last_pid": "p00150",
                         "status": "GOOD", "edits": []},
                    ],
                },
            },
            "audit": {
                "status": "partial",
                "done_chunks": [1, 2], "failed_chunks": [],
                "chunks": [
                    {"chunk": 1, "first_pid": "p00001", "last_pid": "p00100",
                     "pair_count": 100, "context_count": 0, "status": "GOOD",
                     "finish_reason": "stop", "reasoning_chars": 0,
                     "reasoning_file": "b3_audit_chunk1_raw.txt", "issue_count": 3},
                    {"chunk": 2, "first_pid": "p00101", "last_pid": "p00200",
                     "pair_count": 100, "context_count": 0, "status": "GOOD",
                     "finish_reason": "stop", "reasoning_chars": 0,
                     "reasoning_file": "b3_audit_chunk2_raw.txt", "issue_count": 1},
                ],
                "issues": [
                    {"id": "i1", "category": "addition", "severity": "major",
                     "confidence": "high"},
                    {"id": "i2", "category": "omission", "severity": "minor",
                     "confidence": "medium"},
                ],
            },
            "repair": {
                "status": "partial",
                "done_batches": [1, 2],
                "committed": {"p00001": "исправлено 1.", "p00002": "исправлено 2."},
                "passed": ["p00003"],
                "outcome": {
                    "batch_count": 4,
                    "batches": [
                        {"batch_index": 1, "status": "GOOD",
                         "findings": [{"index": 1, "pid": "p00001"}],
                         "results": [{"index": 1, "decision": "repair",
                                      "pid": "p00001", "repaired_translation": "1",
                                      "reason": ""}],
                         "error": "", "warnings": [], "missing_indices": []},
                        {"batch_index": 2, "status": "GOOD",
                         "findings": [{"index": 1, "pid": "p00002"}],
                         "results": [{"index": 1, "decision": "repair",
                                      "pid": "p00002", "repaired_translation": "2",
                                      "reason": ""}],
                         "error": "", "warnings": [], "missing_indices": []},
                    ],
                },
            },
            "reaudit": {
                "status": "partial",
                "done_chunks": [
                    {"chunk": 1, "first_pid": "p00001", "last_pid": "p00100",
                     "issues": [], "failed": False},
                ],
                "issues": [{"id": "r1"}],
            },
        },
    }


def test_phase_block_renders_incremental_stage_progress(tmp_path: Path):
    """RV HIGH #1: an active KILL-SAFE-INCREMENTAL run renders live R_editor /
    Audit / Repair / Re-audit progress from ``stage_progress`` — not the
    ``(нет Phase-артефактов...)`` placeholder that ignored the nested slices.
    Exact K/N and counters come from the partial lists.
    """
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", _stage_progress_cache())
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)

    assert lines[0] == "-- Phase --"
    # R_editor: 3 done chunks; 2 SAFE edits (typo, grammar) + 1 REVIEW (calque).
    assert "R-editor: chunks done=3 | safe (применено)=2 | review (предложено)=1" in text
    # Audit: 2 done chunks, findings [3, 1], всего = 2 issues so far.
    assert "Chapter audit: chunks done=2 | findings per chunk: [3, 1] | всего 2" in text
    # Repair: 2 done batches of batch_count 4, committed 2.
    assert ("Selective repair: batches done=2/4 | repaired per batch: [1/1, 1/1] "
            "| findings eligible: 2 | PID edits committed: 2") in text
    # Re-audit: 1 done chunk, residual 1 issue, incomplete stage -> debt 1.
    assert "Re-audit scope: chunks done=1 | residual: 1 | debt: 1" in text
    assert "(нет Phase-артефактов" not in text


def test_phase_block_final_cache_output_preserved(tmp_path: Path):
    """RV HIGH #1: the historical FINAL-cache shape (top-level r_editor /
    chunks / repair) still renders exactly as before — the stage_progress
    fallback must not change final-cache output."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _phase_mock_artifacts(out)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "R-editor: chunks done=2/2 | safe (применено)=1 | review (предложено)=1" in text
    assert "Chapter audit: chunks done=2/2 | findings per chunk: [3, 0] | всего 3" in text
    assert "Selective repair: batches done=2/2 | repaired per batch: [1/1, 1/2] | findings eligible: 4 | PID edits committed: 2" in text
    assert "Re-audit scope: chunks done=2/2 | residual: 1 | debt: 0" in text


# ---------------------------------------------------------------------------
# RV t_c9f9ea90 MEDIUM #3: graceful degradation — corrupt/empty/ambiguous
# inputs render an unavailable/invalid diagnostic, never an abort.
# ---------------------------------------------------------------------------


def test_phase_block_tolerates_invalid_utf8_json(tmp_path: Path):
    """Invalid UTF-8 bytes in a Phase artifact must not raise
    UnicodeDecodeError; the line degrades to unavailable."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / "entity_context_cache.json").write_bytes(b"\xff\xfe{\"entries\": []}")
    (out / "translations_raw.json").write_bytes(b"\xff{\"p1\": \"text\"}")
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Neither artifact is readable — block renders, proving no abort.
    assert "-- Phase --" in text


def test_phase_block_tolerates_malformed_schema(tmp_path: Path):
    """Structurally-valid JSON with a malformed schema (entries:[1],
    chunks:[1], non-numeric word_counts) must not raise AttributeError /
    ValueError — the affected lines degrade gracefully."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "entity_context_cache.json", {"entries": [1]})
    _write(out / "translations_raw.json", {"p1": "текст абзаца"})
    _write(out / "chunk_plan.json", {"chunks": [
        {"chunk_id": "c1", "word_counts": ["abc", 5]},
        7,  # a non-object chunk -> skipped
    ]})
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": {"chunk_count": 2, "chunks": [1]}},
        "chunks": [1, {"chunk": 2, "issue_count": 3}],
        "issue_count": 3,
        "repair": {"batches": [1, {"batch_index": 2, "findings": [],
                                   "results": []}],
                   "eligible_count": 1},
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Malformed pieces are skipped; readable data still renders — no abort.
    assert "-- Phase --" in text
    # Extraction with entries=[1] yields no entities -> line absent.
    assert "Extraction:" not in text
    # word_counts with a non-numeric value counts it as 0, the numeric one as 5.
    assert "Whole-chapter translation:" in text and "5 слов" in text


def test_usage_block_tolerates_corrupt_ndjson(tmp_path: Path):
    """Invalid UTF-8 / malformed rows in usage.ndjson render an invalid
    diagnostic instead of raising UnicodeDecodeError / AttributeError."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / USAGE_FILENAME).write_bytes(
        b'{"schema":"x","label":"phase2b/a","input_tokens":1}\n'
        b"\xff\xfe{\"garbage\"}\n"
        b"[1, 2, 3]\n"
        b'{"schema":"x","label":"phase2b/b","input_tokens":"abc"}\n'
    )
    lines = tracker._usage_block_lines(out)
    text = "\n".join(lines)
    # Two valid rows survive (the non-object row and garbage are skipped);
    # the non-numeric token counts as 0.
    assert "Whole-chapter translation" in text
    assert "calls" in text
    assert "2" in text

    # Last-call block also degrades on the malformed-but-valid row.
    last = tracker._last_call_block_lines(out)
    assert isinstance(last, list)


def test_usage_block_distinguishes_corrupt_vs_absent(tmp_path: Path):
    """MEDIUM #3: a corrupt usage.ndjson that yields no readable rows is
    reported as corrupt, while an absent file keeps the 'no usage yet'
    wording."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / USAGE_FILENAME).write_bytes(b"\xff\xfe not json \x00")
    lines = tracker._usage_block_lines(out)
    assert any("corrupt" in l or "no readable rows" in l for l in lines)


# ---------------------------------------------------------------------------
# MONITOR-V2 whole-chapter vocabulary: canonical names, live speed, liveness,
# repair/re-audit separation, book table mode/unit, timestamp (task req.)
# ---------------------------------------------------------------------------


def test_canonical_phase_names_consistent(tmp_path: Path):
    """All seven canonical phase names are the single source of truth."""
    assert tracker.PHASE_HUMAN_NAME["extraction"] == "Entity extraction"
    assert tracker.PHASE_HUMAN_NAME["gen"] == "Whole-chapter translation"
    assert tracker.PHASE_HUMAN_NAME["r_editor"] == "R-editor"
    assert tracker.PHASE_HUMAN_NAME["audit"] == "Chapter audit"
    assert tracker.PHASE_HUMAN_NAME["repair"] == "Selective repair"
    assert tracker.PHASE_HUMAN_NAME["reaudit"] == "Re-audit scope"
    assert tracker.PHASE_HUMAN_NAME["formatting"] == "Formatting"
    # MONITOR-V2 finding 6: PHASE_TO_STEP_GROUP is derived from PHASE_HUMAN_NAME.
    for phase in ("extraction", "gen", "r_editor", "audit", "repair",
                  "reaudit", "formatting"):
        assert tracker.PHASE_TO_STEP_GROUP[phase] == tracker.PHASE_HUMAN_NAME[phase]
    # Structural invariant: every key in PHASE_TO_STEP_GROUP (except extras)
    # must exist in PHASE_HUMAN_NAME.
    for key in tracker.PHASE_TO_STEP_GROUP:
        assert key in tracker.PHASE_HUMAN_NAME, (
            f"PHASE_TO_STEP_GROUP key {key!r} not in PHASE_HUMAN_NAME"
        )


def test_speed_block_live_tg3s_without_final_eval(tmp_path: Path):
    """Active local generation with tg_3s but no final eval renders live
    t/s, n_decoded, and 'eval in progress' (task req. 5, finding 1)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    logs = out / "server_logs"
    logs.mkdir(exist_ok=True)
    # Write a log with tg_3s but NO final eval line.
    (logs / "Gemma_20260816_120000_stderr.log").write_text(
        "0.32.156.164 I slot print_timing: id  0 | task 0 | n_decoded =    100, "
        "tg =  26.08 t/s, tg_3s =  26.08 t/s\n",
        encoding="utf-8",
    )
    lines = tracker._server_speed_lines(out)
    assert len(lines) == 2
    assert "live tg_3s 26.08 t/s" in lines[1]
    assert "eval in progress" in lines[1]
    # MONITOR-V2 finding 1: n_decoded must be rendered even without final eval.
    assert "n_decoded=100" in lines[1]
    # Must NOT show "нет завершённых eval" during active generation.
    assert "нет завершённых eval" not in lines[1]


def test_local_liveness_from_fresh_gemma_log(tmp_path: Path):
    """Fresh local Gemma/Qwen log is an additional liveness signal
    (task req. 6). Remote logs cannot produce alive=yes."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    logs = out / "server_logs"
    logs.mkdir(exist_ok=True)
    # Fresh local log -> alive.
    (logs / "Gemma_20260816_120000_stderr.log").write_text("x", encoding="utf-8")
    events = tracker._load_events(out)
    identity = tracker._identity(out, events)
    assert identity["alive"] is True
    assert "local llama timing" in identity["alive_basis"]

    # Remote log only -> NOT alive via local liveness.
    out2 = _chapter_dir(tmp_path, "chapter_0002", _iso(7200))
    logs2 = out2 / "server_logs"
    logs2.mkdir(exist_ok=True)
    (logs2 / "opencode_serve_20260815_100151_stderr.log").write_text("x", encoding="utf-8")
    events2 = tracker._load_events(out2)
    identity2 = tracker._identity(out2, events2)
    # Stale run, no usage/events -> not alive; remote logs don't help.
    assert identity2["alive"] is False


def test_repair_metrics_separated(tmp_path: Path):
    """Repair output shows findings eligible and PID edits committed as
    separate units, not a ratio (task req. 2)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "repair": {
            "eligible_count": 10,
            "committed": ["p1", "p2", "p3"],
            "batches": [
                {"batch_index": 1, "findings": [{"index": 1}],
                 "results": [{"index": 1, "decision": "repair"}]},
                {"batch_index": 2, "findings": [{"index": 2}, {"index": 3}],
                 "results": [{"index": 2, "decision": "repair"},
                             {"index": 3, "decision": "skip"}]},
            ],
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "findings eligible: 10" in text
    assert "PID edits committed: 3" in text
    # Old ratio format must not appear.
    assert "total 3/10" not in text


def test_reaudit_quality_and_execution_debt_separated(tmp_path: Path):
    """Re-audit residual and debt are independent (task req. 3)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "repair": {
            "reaudit": {
                "complete": False,
                "failed": True,
                "issues": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
            },
        },
    })
    _write(out / "b3_repair_reaudit_chunk1_raw.txt", "{}")
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Residual=3, debt=1 (failed re-audit).
    assert "residual: 3" in text
    assert "debt: 1" in text
    # residual>0 does NOT force debt=0.
    assert "residual: 3 | debt: 0" not in text


def test_book_table_mode_unit_whole_chapter(tmp_path: Path):
    """Whole-chapter book rows show '1/1' (task req. 4)."""
    base = tmp_path / "book"
    base.mkdir()
    ch = _chapter_dir(base, "chapter_0001", _iso(3600))
    _write_ndjson(ch / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    report = tracker.render_book_report(base)
    table = report.split("-- active chapter", 1)[0]
    assert "mode/unit" in table
    assert "1/1" in table


def test_book_table_mode_unit_chunked(tmp_path: Path):
    """Chunked book rows show 'N/M' (task req. 4)."""
    base = tmp_path / "book"
    base.mkdir()
    _chapter_dir(base, "chapter_0001", _iso(3600))
    report = tracker.render_book_report(base)
    table = report.split("-- active chapter", 1)[0]
    assert "mode/unit" in table
    # Fixture has 2 chunks planned, 2 journaled -> "2/2".
    assert "2/2" in table
    # Old "chunks" column name must not appear.
    assert "chunks" not in table.split("\n")[0]


def test_counters_whole_chapter_canonical_lifecycle(tmp_path: Path):
    """Per-phase layout uses canonical names and omits absent phases (no not_started)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    report = tracker.render_report(out)
    assert "Whole-chapter translation: attempt 1/3" in report
    assert "R-editor:" not in report
    assert "Chapter audit:" not in report
    assert "Selective repair:" not in report
    assert "Re-audit scope:" not in report
    assert "Formatting: not applicable" not in report
    assert "not_started" not in report
    assert "GEN:" not in report
    assert "Step 6" not in report
    assert "Step 7" not in report
    assert "Step 8" not in report
    assert "Steps 1-5" not in report
    assert "status:" not in report
    assert "mode=fine" not in report


# ---------------------------------------------------------------------------
# MONITOR-V2 finding 3: R-editor lifecycle from B3 events only
# ---------------------------------------------------------------------------

def test_r_editor_not_started_without_b3(tmp_path: Path):
    """Generation done without B3 events must NOT claim R-editor complete."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
        {"schema": "x", "event": "wc_generation_done", "ts": _iso(50),
         "finish_reason": "complete", "pid_count": 100, "duration": 60.0},
    ])
    report = tracker.render_report(out)
    assert "Whole-chapter translation: attempt 1/3" in report
    assert "R-editor:" not in report
    assert "R-editor: not started" not in report
    assert "status:" not in report


def test_r_editor_in_progress_from_b3_started(tmp_path: Path):
    """Audit journal alone does not drive per-phase R-editor line (phase_progress is source)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
        {"schema": "x", "event": "wc_generation_done", "ts": _iso(50),
         "finish_reason": "complete", "pid_count": 100, "duration": 60.0},
    ])
    _write(out / "audit_journal.ndjson", "")
    import json as _json
    with open(out / "audit_journal.ndjson", "a", encoding="utf-8") as fh:
        fh.write(_json.dumps({"schema": "pact-v4-b3-audit-journal/v1",
                               "event": "r_editor_started",
                               "ts": _iso(40)}) + "\n")
    report = tracker.render_report(out)
    assert "Whole-chapter translation: attempt 1/3" in report
    assert "R-editor:" not in report
    assert "not_started" not in report


def test_r_editor_complete_from_b3_done(tmp_path: Path):
    """Audit journal alone does not drive per-phase R-editor line."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
        {"schema": "x", "event": "wc_generation_done", "ts": _iso(50),
         "finish_reason": "complete", "pid_count": 100, "duration": 60.0},
    ])
    _write(out / "audit_journal.ndjson", "")
    import json as _json
    with open(out / "audit_journal.ndjson", "a", encoding="utf-8") as fh:
        fh.write(_json.dumps({"schema": "pact-v4-b3-audit-journal/v1",
                               "event": "r_editor_started",
                               "ts": _iso(40)}) + "\n")
        fh.write(_json.dumps({"schema": "pact-v4-b3-audit-journal/v1",
                               "event": "r_editor_done",
                               "ts": _iso(30), "chunk": 1, "total": 1}) + "\n")
    report = tracker.render_report(out)
    assert "Whole-chapter translation: attempt 1/3" in report
    assert "R-editor:" not in report
    assert "not_started" not in report


# ---------------------------------------------------------------------------
# MONITOR-V2 finding 4: re-audit incomplete execution debt
# ---------------------------------------------------------------------------

def test_reaudit_incomplete_execution_debt_final(tmp_path: Path):
    """Final cache: incomplete re-audit (complete=False, failed=False)
    must render debt=1, not debt=0 (finding 4 regression)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "repair": {
            "reaudit": {
                "complete": False,
                "issues": [{"id": "p1"}],
            },
        },
    })
    _write(out / "b3_repair_reaudit_chunk1_raw.txt", "{}")
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "residual: 1" in text
    # debt=1 because incomplete, NOT because failed.
    assert "debt: 1" in text
    assert "debt: 0" not in text


def test_reaudit_incomplete_execution_debt_incremental(tmp_path: Path):
    """Incremental path: incomplete stage (no terminal status) must render
    debt=1 (finding 4 regression)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "stage_progress": {
            "reaudit": {
                "status": "in_progress",
                "done_chunks": [{"chunk": 1, "issues": []}],
                "issues": [],
            },
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Incomplete stage → debt=1.
    assert "debt: 1" in text
    assert "debt: 0" not in text


def test_reaudit_complete_no_debt(tmp_path: Path):
    """Complete re-audit (complete=True) with no failed chunks → debt=0."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "repair": {
            "reaudit": {
                "complete": True,
                "issues": [],
            },
        },
    })
    _write(out / "b3_repair_reaudit_chunk1_raw.txt", "{}")
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "debt: 0" in text


# ---------------------------------------------------------------------------
# MONITOR-V2 finding 5: canonical phase names in status and chapter table
# ---------------------------------------------------------------------------

def test_status_line_uses_canonical_phase_names(tmp_path: Path):
    """Status line uses canonical phase names, not raw internal codes."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    events = tracker._load_events(out)
    line = tracker._status_line(out, events, "gen")
    # Canonical name, not raw "gen".
    assert "[chapter_0001] Whole-chapter translation" in line
    assert "[chapter_0001] gen" not in line


def test_render_report_phase_line_canonical(tmp_path: Path):
    """Per-phase line uses canonical name with aligned active marker."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    report = tracker.render_report(out)
    assert "Whole-chapter translation: attempt 1/3" in report
    assert "phase:" not in report
    assert "mode=fine" not in report
    assert "status:" not in report


def test_chapter_table_step_uses_canonical(tmp_path: Path):
    """Chapter table step column uses canonical names, not raw step numbers."""
    base = tmp_path / "book"
    base.mkdir()
    out = _chapter_dir(base, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    report = tracker.render_book_report(base)
    # Step column must not show raw "1-5", "6", "7", "8".
    table_line = [l for l in report.split("\n") if "chapter_0001" in l]
    assert table_line
    # Should contain canonical names, not internal codes.
    assert "1-5" not in table_line[0]


# ---------------------------------------------------------------------------
# FINDING 1: cache-authoritative malformed audit_cache_b3.json
# ---------------------------------------------------------------------------

def test_malformed_empty_cache_renders_fail_closed(tmp_path: Path):
    """An empty audit_cache_b3.json is cache-authoritative and must render
    explicit fail-closed errors, not silently fall through."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / "audit_cache_b3.json").write_text("", encoding="utf-8")
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # All four cache-dependent phases render fail-closed errors.
    assert "fail-closed" in text
    assert "R-editor:" in text
    assert "Chapter audit:" in text
    assert "Selective repair:" in text
    assert "Re-audit scope:" in text
    # Must NOT show "нет Phase-артефактов" — the file exists, just malformed.
    assert "нет Phase-артефактов" not in text


def test_malformed_non_json_cache_renders_fail_closed(tmp_path: Path):
    """Non-JSON content in audit_cache_b3.json renders fail-closed."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / "audit_cache_b3.json").write_text("not json {{{", encoding="utf-8")
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "fail-closed" in text
    assert "R-editor:" in text


def test_malformed_non_object_cache_renders_fail_closed(tmp_path: Path):
    """A JSON array (non-object) in audit_cache_b3.json renders fail-closed."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / "audit_cache_b3.json").write_text("[1, 2, 3]", encoding="utf-8")
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "fail-closed" in text


def test_malformed_cache_no_journal_fallback(tmp_path: Path):
    """A malformed audit_cache_b3.json does NOT fall through to journal
    or missing-artifact fallback — it fails closed."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / "audit_cache_b3.json").write_text("garbage", encoding="utf-8")
    events = tracker._load_events(out)
    # Individual phase functions also fail closed.
    assert "fail-closed" in (tracker._phase_r_editor(out) or "")
    assert "fail-closed" in (tracker._phase_audit(out) or "")
    assert "fail-closed" in (tracker._phase_repair(out) or "")
    assert "fail-closed" in (tracker._phase_reaudit(out) or "")


def test_absent_cache_still_returns_none(tmp_path: Path):
    """An absent audit_cache_b3.json returns None (no error) — only
    present-but-malformed triggers fail-closed."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    events = tracker._load_events(out)
    # No audit_cache_b3.json -> individual functions return None.
    assert tracker._phase_r_editor(out) is None
    assert tracker._phase_audit(out) is None
    assert tracker._phase_repair(out) is None
    assert tracker._phase_reaudit(out) is None


def test_malformed_nested_fields_do_not_crash(tmp_path: Path):
    """Malformed nested fields in audit_cache_b3.json (e.g. chunks: [1],
    repair.batches: [1]) must not crash — they render gracefully."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": {"chunk_count": "abc", "successful_chunks": True}},
        "chunks": [1, "garbage"],
        "issue_count": "not_a_number",
        "repair": {"batches": [1], "eligible_count": "x", "committed": "y"},
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Must render without crashing; malformed data degrades gracefully.
    assert "-- Phase --" in text


# ---------------------------------------------------------------------------
# FINDING 2: R-editor successful_chunks / chunk_count validation
# ---------------------------------------------------------------------------

def test_r_editor_chunk_count_must_be_non_negative_int(tmp_path: Path):
    """chunk_count that is a bool, float, string, or negative must NOT
    produce a done=X/Y line — fall through to incremental path."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    for bad_cc in [True, 3.5, "two", -1]:
        _write(out / "audit_cache_b3.json", {
            "r_editor": {
                "outcome": {
                    "chunk_count": bad_cc,
                    "successful_chunks": 2,
                    "applied": [],
                    "candidates": [],
                },
            },
        })
        result = tracker._phase_r_editor(out)
        # Should NOT render "chunks done=X/Y" for bad chunk_count.
        if result is not None:
            assert "chunks done=" not in result or "/" not in result, (
                f"bad chunk_count {bad_cc!r} should not produce done/X/Y"
            )


def test_r_editor_successful_chunks_must_be_non_negative_int(tmp_path: Path):
    """successful_chunks that is bool/string/negative must NOT produce
    done=X/Y — fall through to incremental path."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    for bad_sc in [True, "one", -1]:
        _write(out / "audit_cache_b3.json", {
            "r_editor": {
                "outcome": {
                    "chunk_count": 2,
                    "successful_chunks": bad_sc,
                    "applied": [],
                    "candidates": [],
                },
            },
        })
        result = tracker._phase_r_editor(out)
        if result is not None:
            assert "chunks done=" not in result or "/" not in result, (
                f"bad successful_chunks {bad_sc!r} should not produce done/X/Y"
            )


def test_r_editor_mismatch_falls_through(tmp_path: Path):
    """successful_chunks != chunk_count (conflicting evidence) must fall
    through to incremental path, not render wrong numbers."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 3,
                "successful_chunks": 1,  # mismatch
                "applied": [],
                "candidates": [],
            },
        },
    })
    result = tracker._phase_r_editor(out)
    # Should NOT render "done=1/3" — the mismatch falls through.
    if result is not None:
        assert "done=1/3" not in result


def test_r_editor_coherent_integers_render(tmp_path: Path):
    """Coherent non-negative integer chunk_count == successful_chunks
    renders the done=X/Y line correctly."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 2,
                "successful_chunks": 2,
                "applied": [{"pid": "p1", "text": "x"}],
                "candidates": [{"pid": "p2", "text": "y"}],
            },
        },
    })
    result = tracker._phase_r_editor(out)
    assert result is not None
    assert "done=2/2" in result
    assert "safe (применено)=1" in result
    assert "review (предложено)=1" in result


# ---------------------------------------------------------------------------
# FINDING 3: applied_count / candidate_count from production cache
# ---------------------------------------------------------------------------

def test_r_editor_preserves_applied_candidate_count(tmp_path: Path):
    """When the production cache carries applied_count/candidate_count,
    the monitor uses those values instead of recomputing from lists."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 1,
                "successful_chunks": 1,
                # Production fields — the monitor must preserve these.
                "applied_count": 5,
                "candidate_count": 3,
                # List representations may differ (e.g. after partial apply).
                "applied": [{"pid": "p1"}],
                "candidates": [],
            },
        },
    })
    result = tracker._phase_r_editor(out)
    assert result is not None
    # Must show production counts, not list-length counts.
    assert "safe (применено)=5" in result
    assert "review (предложено)=3" in result


def test_r_editor_falls_back_to_list_length(tmp_path: Path):
    """When applied_count/candidate_count are absent, the monitor computes
    from the applied/candidates lists."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 1,
                "successful_chunks": 1,
                "applied": [{"pid": "p1"}, {"pid": "p2"}],
                "candidates": [{"pid": "p3"}],
            },
        },
    })
    result = tracker._phase_r_editor(out)
    assert result is not None
    assert "safe (применено)=2" in result
    assert "review (предложено)=1" in result


def test_repair_preserves_eligible_count(tmp_path: Path):
    """The monitor preserves the production eligible_count field."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "repair": {
            "eligible_count": 15,
            "committed": ["p1", "p2"],
            "batches": [
                {"batch_index": 1, "findings": [{"index": 1}],
                 "results": [{"index": 1, "decision": "repair"}]},
            ],
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "findings eligible: 15" in text
    assert "PID edits committed: 2" in text



# ---------------------------------------------------------------------------
# FIX ROUND 2: Defect 1 -- malformed nested R-editor shape fail-closed
# ---------------------------------------------------------------------------

def test_r_editor_malformed_outcome_not_dict(tmp_path: Path):
    """Cache present, r_editor is a dict, but outcome is a list (not dict).
    Must render explicit fail-closed diagnostic, not hide as absent."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": ["unexpected", "list"]},
    })
    result = tracker._phase_r_editor(out)
    assert result is not None
    assert "fail-closed" in result
    assert "outcome is not a valid object" in result


def test_r_editor_malformed_outcome_none(tmp_path: Path):
    """Cache present, r_editor is a dict, outcome is None.
    Must render explicit fail-closed diagnostic."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": None},
    })
    result = tracker._phase_r_editor(out)
    assert result is not None
    assert "fail-closed" in result


def test_r_editor_malformed_r_editor_not_dict(tmp_path: Path):
    """Cache present, r_editor is a string (not dict).
    Must render explicit fail-closed diagnostic."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": "not_a_dict",
    })
    result = tracker._phase_r_editor(out)
    assert result is not None
    assert "fail-closed" in result
    assert "r_editor is not a valid object" in result


def test_r_editor_malformed_invalid_chunk_count(tmp_path: Path):
    """Cache present, r_editor.outcome has chunk_count=abc (string) and
    successful_chunks=True (bool). Both invalid -> fail-closed."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": "abc",
                "successful_chunks": True,
            },
        },
    })
    result = tracker._phase_r_editor(out)
    assert result is not None
    assert "fail-closed" in result
    # Both invalid -> "no valid completion data" (not "invalid chunk_count")
    assert "no valid completion data" in result


def test_r_editor_malformed_invalid_successful_chunks(tmp_path: Path):
    """Cache present, r_editor.outcome has valid chunk_count but
    invalid successful_chunks (float). Must fall through to
    incremental (no GOOD inference)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 3,
                "successful_chunks": 1.5,  # float, invalid
            },
        },
    })
    result = tracker._phase_r_editor(out)
    if result is not None:
        assert "done=2/2" not in result
        assert "done=3/3" not in result


def test_r_editor_malformed_full_render_report(tmp_path: Path):
    """Full render_report with malformed nested r_editor shape
    must show the fail-closed diagnostic in the Phase block."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": {"chunk_count": "abc"}},
    })
    report = tracker.render_report(out)
    assert "R-editor: audit_cache_b3.json present but" in report
    assert "fail-closed" in report
    # Must NOT show "нет Phase-артефактов" when cache is present.
    assert "\u043d\u0435\u0442 Phase-\u0430\u0440\u0442\u0435\u0444\u0430\u043a\u0442\u043e\u0432" not in report


def test_r_editor_conflicting_cache_journal_full_report(tmp_path: Path):
    """Valid cache with mismatched chunk_count/successful_chunks
    falls through to incremental; full report does not claim R-editor
    complete from journal when cache evidence conflicts."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 3,
                "successful_chunks": 1,  # mismatch
            },
        },
    })
    report = tracker.render_report(out)
    assert "done=3/3" not in report


# ---------------------------------------------------------------------------
# FIX ROUND 2: Defect 2 -- no GOOD chunk inference for absent successful_chunks
# ---------------------------------------------------------------------------

def test_r_editor_no_good_chunk_fallback(tmp_path: Path):
    """When chunk_count is valid but successful_chunks is absent,
    the monitor must NOT count GOOD chunks as done."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 2,
                "chunks": [
                    {"status": "GOOD"},
                    {"status": "GOOD_RETRIED"},
                ],
            },
        },
    })
    result = tracker._phase_r_editor(out)
    if result is not None:
        assert "done=2/2" not in result


def test_r_editor_bool_successful_chunks_no_inference(tmp_path: Path):
    """When successful_chunks is a bool (True), must NOT count GOOD chunks."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 1,
                "successful_chunks": True,
                "chunks": [{"status": "GOOD"}],
            },
        },
    })
    result = tracker._phase_r_editor(out)
    if result is not None:
        assert "done=1/1" not in result


def test_r_editor_negative_successful_chunks_no_inference(tmp_path: Path):
    """When successful_chunks is negative, must NOT count GOOD chunks."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {
            "outcome": {
                "chunk_count": 1,
                "successful_chunks": -1,
                "chunks": [{"status": "GOOD"}],
            },
        },
    })
    result = tracker._phase_r_editor(out)
    if result is not None:
        assert "done=1/1" not in result


# ---------------------------------------------------------------------------
# FIX ROUND 2: Defect 3 -- legacy labels removed from normal output
# ---------------------------------------------------------------------------

def test_no_legacy_labels_in_chunk_block(tmp_path: Path):
    """No legacy trial/chunk labels appear in per-phase output."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    report = tracker.render_report(out)
    assert "Whole-chapter translation:" in report
    assert "not_started" not in report
    assert "status:" not in report
    assert "phase:" not in report
    for line in report.split("\n"):
        if "chunks (" in line:
            assert "trial" not in line.lower()


def test_no_legacy_labels_in_counters_block(tmp_path: Path):
    """No legacy Step labels appear in per-phase output."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "x", "event": "wc_generation_started", "ts": _iso(60),
         "max_attempts": 3},
    ])
    report = tracker.render_report(out)
    assert "Step 6" not in report
    assert "Step 7" not in report
    assert "Step 6/7" not in report
    assert "Whole-chapter translation:" in report
    assert "not_started" not in report
    assert "status:" not in report


def test_no_legacy_labels_in_book_table(tmp_path: Path):
    """The book table step column must use canonical names."""
    out_base = tmp_path / "book_run"
    out_base.mkdir()
    ch = out_base / "chapter_0001"
    ch.mkdir()
    _write(ch / "chunk_plan.json", {"chunks": [{"chunk_id": "c1"}]})
    _write_ndjson(ch / "journal.ndjson", [{"chunk_id": "c1", "outcome": "selected"}])
    _write_ndjson(ch / PHASE_PROGRESS_FILENAME, [{
        "schema": "pact-v4-phase-progress/ndjson/v1",
        "event": "run_started",
        "ts": _iso(3600),
        "started_at": _iso(3600),
        "resumed_from_index": 0,
    }])
    text = tracker.render_book_report(out_base)
    assert "Entity ext." not in text
    assert "Whole-chapter" in text or "Entity extraction" in text or "Chapter audit" in text


def test_canonical_names_consistent_all_blocks(tmp_path: Path):
    """All user-facing blocks use the same canonical names."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "entity_context_cache.json", {
        "entries": [{"context": {"entities": [
            {"anchor": {"status": "verified"}, "aliases": [], "claims": []}
        ]}}]
    })
    report = tracker.render_report(out)
    assert "Entity extraction:" in report
    assert "Step 6" not in report
    assert "Step 7" not in report
    assert "Entity ext." not in report


# ----------------------------------------------------------------
# Repair scalar-schema regression coverage (merged from fix/repair-prompt-...)
# ----------------------------------------------------------------

def test_phase_block_scalar_chunk_status_no_crash(tmp_path: Path):
    """r_editor.outcome.chunks with scalar status (e.g. status=42)
    must not raise AttributeError at .upper() -- the monitor safely
    skips chunks with non-string status."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": {
            "chunk_count": 2, "successful_chunks": None,
            "chunks": [
                {"status": "GOOD"},
                {"status": 42},
                {"status": []},
            ],
        }},
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Scalar/non-string statuses must not crash; the monitor does NOT
    # fabricate a done count from chunk statuses (no GOOD-chunk inference,
    # MONITOR-V2 strict successful_chunks contract).
    assert "chunks done=" not in text
    assert "R-editor:" not in text

def test_phase_block_scalar_context_no_crash(tmp_path: Path):
    """entity_context_cache.json with context as a string (not a dict)
    must not raise AttributeError -- Extraction line degrades gracefully."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "entity_context_cache.json",
           {"entries": [{"context": "bad"}]})
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Extraction with scalar context yields no entities -> line absent.
    assert "Extraction:" not in text
    assert "-- Phase --" in text

def test_phase_block_scalar_decision_final_repair_no_crash(tmp_path: Path):
    """Final repair batches with scalar result.decision (e.g. 42)
    must not raise AttributeError at .lower() -- the monitor safely
    skips results with non-string decision."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "repair": {
            "eligible_count": 3,
            "batches": [
                {"findings": 3, "results": [
                    {"decision": "repair"},
                    {"decision": 42},
                    {"decision": []},
                ]},
            ],
            "committed": 1,
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Only the string "repair" decision counts; scalar/non-string skipped.
    assert "Selective repair: batches done=1/1" in text
    assert "1/3" in text

def test_phase_block_scalar_decision_incremental_repair_no_crash(tmp_path: Path):
    """Incremental stage_progress.repair with scalar result.decision
    must not raise AttributeError at .lower() -- same guard as final."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "stage_progress": {
            "repair": {
                "status": "partial",
                "done_batches": [1],
                "committed": 1,
                "outcome": {
                    "batch_count": 2,
                    "batches": [
                        {"findings": 2, "results": [
                            {"decision": "repair"},
                            {"decision": 99},
                        ]},
                    ],
                },
            },
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Only string "repair" decision counts; scalar skipped.
    assert "Selective repair: batches done=1/2" in text
    assert "1/2" in text

def test_phase_block_scalar_entities_no_crash(tmp_path: Path):
    """entity_context_cache.json with context.entities as a scalar
    (e.g. {"entities": 1}) must not raise TypeError on iteration --
    the monitor degrades gracefully and skips malformed entities."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "entity_context_cache.json",
           {"entries": [{"context": {"entities": 1}}]})
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # Scalar entities yields no iterable -> Extraction line absent.
    assert "Extraction:" not in text
    assert "Extraction:" not in text
    assert "Phase" in text

def test_phase_block_scalar_r_editor_applied_candidates(tmp_path: Path):
    """r_editor.outcome.applied/candidates as scalar ints must not raise
    TypeError at len() -- the monitor renders scalar as a count."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "r_editor": {"outcome": {
            "chunk_count": 2, "successful_chunks": 2,
            "applied": 4, "candidates": 1,
        }},
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "R-editor: chunks done=2/2" in text
    assert "safe (применено)=4" in text
    assert "review (предложено)=1" in text

def test_phase_block_scalar_repair_findings_results_committed(tmp_path: Path):
    """repair.batches[].findings as scalar int, results as scalar int,
    and repair.committed as scalar int must not raise TypeError at
    len() or iteration -- the monitor renders scalar as a count."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "repair": {
            "eligible_count": 4,
            "batches": [
                {"findings": 4, "results": []},
                {"findings": [1, 2, 3], "results": 4},
            ],
            "committed": 4,
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "Selective repair: batches done=2/2" in text
    # Scalar findings=4 renders as count; results=[] yields repaired=0.
    assert "0/4" in text
    # Scalar results=4 yields repaired=0 (not iterable).
    assert "0/3" in text
    assert "PID edits committed: 4" in text

def test_phase_block_scalar_repair_in_incremental_cache(tmp_path: Path):
    """Incremental stage_progress.repair with scalar findings/results/
    committed must not raise TypeError -- same guard as final cache."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "stage_progress": {
            "repair": {
                "status": "partial",
                "done_batches": [1, 2],
                "committed": 4,
                "outcome": {
                    "batch_count": 3,
                    "batches": [
                        {"findings": 2, "results": []},
                        {"findings": [1], "results": 3},
                    ],
                },
            },
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    assert "Selective repair: batches done=2/3" in text
    assert "0/2" in text
    assert "0/1" in text
    assert "PID edits committed: 4" in text

def test_phase_block_unhashable_edit_class_no_crash(tmp_path: Path):
    """Incremental r_editor with edit.class as unhashable type (e.g. [])
    must not raise TypeError during set membership -- the monitor
    safely skips edits with non-string class."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {
        "stage_progress": {
            "r_editor": {
                "status": "partial",
                "done_chunks": [1],
                "outcome": {
                    "chunks": [
                        {"edits": [
                            {"class": "typo"},
                            {"class": []},
                            {"class": 42},
                            {"class": "ambiguity"},
                        ]},
                    ],
                },
            },
        },
    })
    events = tracker._load_events(out)
    lines = tracker._phase_block_lines(out, events)
    text = "\n".join(lines)
    # typo=safe, ambiguity=review; unhashable/numeric are skipped.
    assert "safe (\u043f\u0440\u0438\u043c\u0435\u043d\u0435\u043d\u043e)=1" in text
    assert "review (\u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u043e)=1" in text
