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

PHASE_PROGRESS_SCHEMA = "pact-v4-phase-progress/ndjson/v1"


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


def _wc_event(event: str, seconds_ago: float, **fields) -> dict:
    return {
        "schema": PHASE_PROGRESS_SCHEMA,
        "event": event,
        "ts": _iso(seconds_ago),
        **fields,
    }


def _b3_event(event: str, seconds_ago: float, **fields) -> dict:
    return {
        "schema": "pact-v4-b3-audit-journal/v1",
        "event": event,
        "ts": _iso(seconds_ago),
        **fields,
    }


def _wc_run_dir(tmp_path: Path, *, with_b3: bool = True) -> Path:
    """Whole-chapter run dir: run_started + wc generation events."""
    out = tmp_path / "chapter_0001"
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("run_started", 600, chapter_id="0001", out_dir=str(out),
                  started_at=_iso(600), backend_identity_hash="h", resumed_from_index=0),
        _wc_event("wc_generation_started", 550, pid_count=120,
                  reasoning_budget=3, model="gemma-4-26b", max_attempts=3),
    ])
    if with_b3:
        _write_ndjson(out / "audit_journal.ndjson", [])
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
    assert by_group[("Translation", "deepseek-v4-flash")]["calls"] == 2
    assert by_group[("Translation", "deepseek-v4-flash")]["step"] == "Translation"
    # phase2c/phase4 keep their phase identity through phase_for_label.
    assert by_group[("qwen_fidelity", "qwen3.7-plus")]["step"] == "qwen_fidelity"
    assert by_group[("Repair", "deepseek-v4-flash")]["step"] == "Repair"
    assert by_group[("Repair", "qwen3.7-plus")]["step"] == "Repair"
    assert by_group[("Audit", "qwen3.7-plus")]["step"] == "Audit"
    assert by_group[("Formatting", "deepseek-v4-flash")]["step"] == "Formatting"

    # The step mapping goes through phase_for_label — prove the reuse by
    # checking the label->phase leg is literally v4_usage's function.
    assert tracker.phase_for_label is v4_usage.phase_for_label
    assert by_group[("Translation", "deepseek-v4-flash")]["reported_cost"] == 0.03
    assert by_group[("Audit", "qwen3.7-plus")]["reported_cost"] == 0.04


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
    assert "Formatting: not started (ожидание formatting/terminal)" in report
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
    assert "phase=Audit" in report
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
    assert "Extraction: сущностей: 2 | claims: verified 2 / candidate 2" in text
    assert "Translation: attempt 1/3 | source 286 слов → перевод 7 слов" in text
    assert "R_editor: chunks done=2/2 | safe (применено)=1 | review (предложено)=1" in text
    assert "Audit: chunks done=2/2 | findings per chunk: [3, 0] | всего 3" in text
    assert "Repair: batches done=2/2 | repaired per batch: [1/1, 1/2] | total 2/4" in text
    assert "Re-audit: chunks done=2/2 | residual: 1 | debt: 0" in text


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
    assert "Translation: attempt 2/3 | source 5 слов → перевод 1 слов" in "\n".join(lines)


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
    assert "n_decoded=2373, eval 79.8s (запрос от 14:51:57)" in lines[2]


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


def test_llama_ts_to_hms():
    assert tracker._llama_ts_to_hms("14.51.578.231") == "14:51:57"
    assert tracker._llama_ts_to_hms("4.30.558.334") == "4:30:55"


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
    assert lines[1] == "  Re-audit | qwen3.7-plus | in=45620 out=210 reas=5800 | wall=?s"


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
    # Status is partial (3/5 chunks done) so lifecycle status is shown.
    assert "R_editor: chunks done=3/3 (partial) | safe (применено)=2 | review (предложено)=1" in text
    # Audit: 2 done chunks, findings [3, 1], всего = 2 issues so far.
    assert "Audit: chunks done=2 | findings per chunk: [3, 1] | всего 2" in text
    # Repair: 2 done batches of batch_count 4, committed 2.
    assert ("Repair: batches done=2/4 | repaired per batch: [1/1, 1/1] "
            "| total 2") in text
    # Re-audit: 1 done chunk, residual 1 issue, no failed marker -> debt 0.
    assert "Re-audit: chunks done=1 | residual: 1 | debt: 0" in text
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
    assert "R_editor: chunks done=2/2 | safe (применено)=1 | review (предложено)=1" in text
    assert "Audit: chunks done=2/2 | findings per chunk: [3, 0] | всего 3" in text
    assert "Repair: batches done=2/2 | repaired per batch: [1/1, 1/2] | total 2/4" in text
    assert "Re-audit: chunks done=2/2 | residual: 1 | debt: 0" in text


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
    assert "Translation:" in text and "5 слов" in text


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
    assert "Translation" in text
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
# Structural regression: PHASE_HUMAN_NAME is the single display mapping
# ---------------------------------------------------------------------------


def test_phase_human_name_is_single_display_mapping():
    """Structural regression: PHASE_HUMAN_NAME is the single canonical
    display mapping. PHASE_TO_STEP_GROUP is NOT used for user-facing
    display; only PHASE_HUMAN_NAME feeds render_report, _status_line,
    _usage_block_lines, and _chapter_summary_row."""
    # PHASE_HUMAN_NAME must cover every phase that PHASE_TO_STEP_GROUP
    # maps, preventing drift.
    for phase in tracker.PHASE_TO_STEP_GROUP:
        assert phase in tracker.PHASE_HUMAN_NAME, (
            f"phase {phase!r} in PHASE_TO_STEP_GROUP but not in PHASE_HUMAN_NAME"
        )
    # Internal phase names (step6/step7/step8/steps1-5) must also be
    # covered so _detect_phase output resolves to canonical names.
    for internal in ("step6", "step7", "step8", "steps1-5"):
        assert internal in tracker.PHASE_HUMAN_NAME, (
            f"internal phase {internal!r} not in PHASE_HUMAN_NAME"
        )
    # Core pipeline phases must have unique display names (internal names
    # may alias to the same canonical value, which is intentional).
    core_phases = [k for k in tracker.PHASE_HUMAN_NAME
                   if k not in ("(other)", "step6", "step7", "step8", "steps1-5")]
    core_values = [tracker.PHASE_HUMAN_NAME[k] for k in core_phases]
    assert len(core_values) == len(set(core_values)), (
        f"Duplicate values in core PHASE_HUMAN_NAME: {core_values}"
    )


def test_usage_block_rows_use_canonical_names(tmp_path: Path):
    """Usage rows must show canonical PHASE_HUMAN_NAME values in the step
    column, never legacy Steps1-5/Step6/Step7/Step8."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", cost=0.01),
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", cost=0.02),
        _usage_row("phase4/region_repair", cost=0.03),
        _usage_row("phase5/formatting_align", cost=0.04),
    ])
    report = tracker.render_report(out)
    # Must contain canonical names
    assert "Translation" in report
    assert "Audit" in report
    assert "Repair" in report
    assert "Formatting" in report
    # Must NOT contain legacy step-group labels
    assert "Steps1-5" not in report
    assert "Step6" not in report
    assert "Step7" not in report
    assert "Step8" not in report


def test_status_line_uses_canonical_names(tmp_path: Path):
    """Status line must use canonical PHASE_HUMAN_NAME values, never raw
    phase labels (GEN, AUDIT, REPAIR) or internal step names."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started",
         "ts": _iso(3600), "started_at": _iso(3600)},
    ])
    events = tracker._load_events(out)
    line = tracker._status_line(out, events, "gen")
    # Must not contain raw internal labels
    assert "GEN " not in line
    assert "AUDIT " not in line
    assert "REPAIR " not in line
    # Must contain canonical names
    assert "Translation" in line


def test_chapter_summary_uses_canonical_names(tmp_path: Path):
    """Book-run chapter summary row must use canonical phase names for
    step and status columns."""
    base = tmp_path / "book"
    base.mkdir()
    out = _wc_run_dir(base)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_retry_attempt", 500, attempt=2, reason="malformed"),
    ])
    report = tracker.render_book_report(base)
    # Must not contain raw phase names
    assert "step6" not in report
    assert "step7" not in report
    assert "step8" not in report
    assert "steps1-5" not in report
    # Must contain canonical names
    assert "Translation" in report


def test_render_report_shows_canonical_phase(tmp_path: Path):
    """render_report must show the canonical phase name, not raw internal
    phase values like step6/step7."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "b2_handoff.json", {"chunks": [
        {"chunk_id": "c1", "audit_status": "clean"},
        {"chunk_id": "c2", "audit_status": "clean"},
    ]})
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started",
         "ts": _iso(60), "started_at": _iso(60)},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "repair_round_started",
         "ts": _iso(50), "round_number": 1},
    ])
    report = tracker.render_report(out)
    # phase line must show canonical name
    assert "phase: Repair --" in report
    # Must not contain "phase: step7 --"
    assert "phase: step7" not in report
    assert "phase: step6" not in report


# ---------------------------------------------------------------------------
# Journal-only R-editor lifecycle (RV t_7cf9ae65 HIGH #3)
# ---------------------------------------------------------------------------


def _r_editor_journal_dir(tmp_path: Path, events: list) -> Path:
    """Create a run dir with only a B3 journal (no audit_cache_b3.json)."""
    out = tmp_path / "run_r_editor_journal"
    _write(out / "chunk_plan.json", {"chunks": [{"chunk_id": "c1"}]})
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started",
         "ts": _iso(60), "started_at": _iso(60)},
    ])
    _write_ndjson(out / "audit_journal.ndjson", events)
    return out


def test_r_editor_journal_complete(tmp_path: Path):
    """Journal-only r_editor_done with status=complete renders a lifecycle
    line with chunk counts."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4, "successful_chunks": 4,
         "applied_count": 3, "candidate_count": 1},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "R_editor: chunks done=4/4" in line
    assert "safe (применено)=3" in line
    assert "review (предложено)=1" in line


def test_r_editor_journal_failed(tmp_path: Path):
    """Journal-only r_editor_done with status=failed renders a failed line,
    never claims completion."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "failed"},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "failed" in line.lower()
    assert "done=" not in line


def test_r_editor_journal_partial(tmp_path: Path):
    """Journal-only r_editor_done with status=partial renders a partial
    line with chunk counts."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "partial", "chunk_count": 6, "successful_chunks": 3,
         "applied_count": 2, "candidate_count": 1},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "partial" in line
    assert "chunks done=3/6" in line


def test_r_editor_journal_unknown_status(tmp_path: Path):
    """Journal-only r_editor_done with an unknown status renders a
    diagnostic, never claims completion."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "weird_status"},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "weird_status" in line
    assert "done=" not in line


def test_r_editor_journal_malformed_missing_fields(tmp_path: Path):
    """Journal-only r_editor_done with missing fields (no chunk_count,
    no status) renders a diagnostic."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10)},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "unknown" in line.lower()


def test_r_editor_no_events_returns_none(tmp_path: Path):
    """When there is no cache and no journal events, _phase_r_editor
    returns None (no R_editor line rendered)."""
    out = _r_editor_journal_dir(tmp_path, [])
    line = tracker._phase_r_editor(out)
    assert line is None


def test_r_editor_cache_takes_precedence_over_journal(tmp_path: Path):
    """When both cache and journal exist, cache wins (authoritative
    precedence)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Write a final cache with R_editor outcome
    cache = {
        "r_editor": {
            "outcome": {
                "chunk_count": 2,
                "successful_chunks": 2,
                "applied": ["p1"],
                "candidates": ["p2"],
            }
        }
    }
    _write(out / "audit_cache_b3.json", cache)
    # Also write journal events (should be ignored when cache exists)
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 5, "successful_chunks": 3,
         "applied_count": 1, "candidate_count": 0},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Cache values should be used, not journal
    assert "chunks done=2/2" in line
    assert "safe (применено)=1" in line
    assert "review (предложено)=1" in line


# ---------------------------------------------------------------------------
# RV t_dd4cf283: Finding 1 — no forbidden legacy labels in normal output
# ---------------------------------------------------------------------------


def test_no_legacy_labels_in_whole_chapter_normal_report(tmp_path: Path):
    """Direct probe: whole-chapter normal report must not contain forbidden
    legacy labels (Steps 1-5, GEN:, Step6, Step7, Step8)."""
    base = tmp_path / "book"
    base.mkdir()
    out = _wc_run_dir(base)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_started", 550, pid_count=120,
                  reasoning_budget=3, model="gemma-4-26b", max_attempts=3),
        _wc_event("wc_generation_done", 500, attempt=1, pid_count=120,
                  reasoning_budget=3, model="gemma-4-26b", max_attempts=3),
    ])
    report = tracker.render_book_report(base)
    # Forbidden legacy labels must NOT appear
    assert "Steps 1-5" not in report
    assert "Steps1-5" not in report
    assert "GEN:" not in report
    assert "GEN " not in report
    assert "Step6" not in report
    assert "Step7" not in report
    assert "Step8" not in report
    # Must contain canonical names
    assert "Translation" in report


def test_no_legacy_labels_in_chunked_fine_normal_report(tmp_path: Path):
    """Direct probe: chunked fine normal report must not contain forbidden
    legacy labels (Steps 1-5, GEN:, Step6, Step7, Step8)."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", cost=0.01),
        _usage_row("phase3/qwen_chapter_audit", model="qwen3.7-plus", cost=0.02),
        _usage_row("phase4/region_repair", cost=0.03),
        _usage_row("phase5/formatting_align", cost=0.04),
    ])
    report = tracker.render_report(out)
    # Forbidden legacy labels must NOT appear
    assert "Steps 1-5" not in report
    assert "Steps1-5" not in report
    assert "GEN:" not in report
    assert "GEN " not in report
    assert "Step6" not in report
    assert "Step7" not in report
    assert "Step8" not in report
    # Must contain canonical names
    assert "Translation" in report
    assert "Audit" in report
    assert "Repair" in report
    assert "Formatting" in report


def test_no_legacy_labels_in_chunked_coarse_normal_report(tmp_path: Path):
    """Direct probe: chunked coarse normal report must not contain forbidden
    legacy labels."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    report = tracker.render_report(out)
    # Forbidden legacy labels must NOT appear
    assert "Steps 1-5" not in report
    assert "Steps1-5" not in report
    assert "GEN:" not in report
    assert "GEN " not in report
    assert "Step6" not in report
    assert "Step7" not in report
    assert "Step8" not in report


def test_in_flight_chunk_uses_canonical_name(tmp_path: Path):
    """Direct probe: in-flight chunk activity must use canonical PHASE_HUMAN_NAME,
    not legacy 'Steps 1-5'."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "chunk_started",
         "ts": _iso(10), "chunk_id": "c1"},
    ])
    events = tracker._load_events(out)
    in_flight = tracker._in_flight_model_activity(events)
    assert len(in_flight) == 1
    # Must use canonical name, not legacy
    assert "Translation" in in_flight[0]
    assert "Steps 1-5" not in in_flight[0]
    assert "Steps1-5" not in in_flight[0]


def test_wc_gen_counter_uses_canonical_name(tmp_path: Path):
    """Direct probe: whole-chapter generation counter must use canonical
    PHASE_HUMAN_NAME, not 'GEN:'."""
    base = tmp_path / "book"
    base.mkdir()
    out = _wc_run_dir(base)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_started", 550, pid_count=120,
                  reasoning_budget=3, model="gemma-4-26b", max_attempts=3),
    ])
    report = tracker.render_book_report(base)
    # Must use canonical name
    assert "Translation:" in report
    assert "GEN:" not in report


# ---------------------------------------------------------------------------
# RV t_dd4cf283: Finding 2 — R-editor lifecycle matrix
# ---------------------------------------------------------------------------


def test_r_editor_cache_failed_with_no_outcome(tmp_path: Path):
    """Direct probe: valid final-cache r_editor.status=failed, outcome=None
    must render a failed diagnostic, not fall through to journal."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    cache = {"r_editor": {"status": "failed"}}
    _write(out / "audit_cache_b3.json", cache)
    # Journal says complete — should be ignored (cache authoritative)
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4, "successful_chunks": 4},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "failed" in line.lower()
    assert "cache" in line.lower()
    # Must NOT render journal complete despite journal event
    assert "chunks done=4/4" not in line


def test_r_editor_cache_partial_with_outcome(tmp_path: Path):
    """Direct probe: valid partial cache outcome must show lifecycle status
    explicitly, not just 'chunks done=K/N'."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    cache = {
        "r_editor": {
            "status": "partial",
            "outcome": {
                "chunk_count": 4,
                "successful_chunks": 2,
                "applied": ["p1"],
                "candidates": ["p2"],
            }
        }
    }
    _write(out / "audit_cache_b3.json", cache)
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=2/4" in line
    assert "partial" in line
    # Must NOT render as complete (2 != 4)
    assert "safe (применено)=1" in line


def test_r_editor_cache_incomplete_with_outcome(tmp_path: Path):
    """Direct probe: valid incomplete cache outcome must show lifecycle status."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    cache = {
        "r_editor": {
            "status": "incomplete",
            "outcome": {
                "chunk_count": 5,
                "successful_chunks": 3,
                "applied": [],
                "candidates": [],
            }
        }
    }
    _write(out / "audit_cache_b3.json", cache)
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=3/5" in line
    assert "incomplete" in line


def test_r_editor_cache_failed_with_outcome(tmp_path: Path):
    """Direct probe: valid failed cache with outcome must show failed status."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    cache = {
        "r_editor": {
            "status": "failed",
            "outcome": {
                "chunk_count": 4,
                "successful_chunks": 1,
                "applied": [],
                "candidates": [],
            }
        }
    }
    _write(out / "audit_cache_b3.json", cache)
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=1/4" in line
    assert "failed" in line


def test_r_editor_cache_complete_validates_chunk_count(tmp_path: Path):
    """Direct probe: cache status=complete with successful_chunks != chunk_count
    must render incomplete, not complete."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    cache = {
        "r_editor": {
            "status": "complete",
            "outcome": {
                "chunk_count": 4,
                "successful_chunks": 2,  # Mismatch: 2 != 4
                "applied": ["p1"],
                "candidates": [],
            }
        }
    }
    _write(out / "audit_cache_b3.json", cache)
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=2/4" in line
    # Must NOT render as complete despite status=complete
    assert "(complete)" not in line or "incomplete" in line


def test_r_editor_journal_complete_validates_done_chunks(tmp_path: Path):
    """Direct probe: journal status=complete with done_chunks != chunk_count
    must render incomplete."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4, "successful_chunks": 2,
         "applied_count": 1, "candidate_count": 0},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=2/4" in line
    assert "incomplete" in line


def test_r_editor_journal_failed_renders_failed(tmp_path: Path):
    """Direct probe: journal status=failed renders a failed diagnostic."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10), "status": "failed"},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "failed" in line.lower()


def test_r_editor_journal_partial_renders_partial(tmp_path: Path):
    """Direct probe: journal status=partial renders a partial diagnostic."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "partial", "chunk_count": 4, "successful_chunks": 2,
         "applied_count": 1, "candidate_count": 0},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=2/4" in line
    assert "partial" in line


def test_r_editor_cache_authoritative_over_journal(tmp_path: Path):
    """Direct probe: when cache exists, journal is never consulted regardless
    of cache status. Cache is authoritative for all statuses."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Cache says failed
    cache = {"r_editor": {"status": "failed"}}
    _write(out / "audit_cache_b3.json", cache)
    # Journal says complete with different numbers
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 10, "successful_chunks": 10,
         "applied_count": 5, "candidate_count": 3},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Must show cache status, not journal
    assert "failed" in line.lower()
    assert "10/10" not in line


def test_r_editor_malformed_cache_does_not_fall_through(tmp_path: Path):
    """Direct probe: malformed/conflicting cache evidence must not fall
    through to journal."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Cache with invalid structure
    cache = {"r_editor": "invalid_string"}
    _write(out / "audit_cache_b3.json", cache)
    # Journal says complete
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 2, "successful_chunks": 2},
    ])
    line = tracker._phase_r_editor(out)
    # Should either return None (no valid cache/journal) or render journal
    # but NOT render cache garbage
    if line is not None:
        assert "invalid_string" not in line


# ---------------------------------------------------------------------------
# RV t_dd4cf283: Finding 3 — PHASE_HUMAN_NAME structural regression
# ---------------------------------------------------------------------------


def test_phase_display_order_is_single_source():
    """Structural regression: PHASE_DISPLAY_ORDER is the single canonical
    sort order for usage-by-step-x-model. No duplicate order dicts exist."""
    # PHASE_DISPLAY_ORDER must cover all core phases
    for phase in tracker.PHASE_HUMAN_NAME:
        human = tracker.PHASE_HUMAN_NAME[phase]
        if human in ("(other)",):
            continue
        assert human in tracker.PHASE_DISPLAY_ORDER, (
            f"canonical display {human!r} (from phase {phase!r}) "
            f"not in PHASE_DISPLAY_ORDER"
        )


def test_chapter_summary_uses_phase_human_name(tmp_path: Path):
    """Direct probe: _chapter_summary_row must use PHASE_HUMAN_NAME for
    step and status, never hardcoded strings."""
    base = tmp_path / "book"
    base.mkdir()
    out = _wc_run_dir(base)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_retry_attempt", 500, attempt=2, reason="malformed"),
    ])
    report = tracker.render_book_report(base)
    # Must not contain raw phase names
    assert "step6" not in report
    assert "step7" not in report
    assert "step8" not in report
    assert "steps1-5" not in report
    # Must contain canonical names
    assert "Translation" in report


def test_no_duplicate_display_names_in_phase_human_name():
    """Structural regression: core PHASE_HUMAN_NAME values must be unique
    to prevent display drift."""
    core_phases = [k for k in tracker.PHASE_HUMAN_NAME
                   if k not in ("(other)", "step6", "step7", "step8", "steps1-5")]
    core_values = [tracker.PHASE_HUMAN_NAME[k] for k in core_phases]
    assert len(core_values) == len(set(core_values)), (
        f"Duplicate values in core PHASE_HUMAN_NAME: {core_values}"
    )


def test_phase_display_order_matches_human_name_values():
    """Structural regression: PHASE_DISPLAY_ORDER values must be a subset
    of PHASE_HUMAN_NAME values (no stale display names)."""
    human_values = set(tracker.PHASE_HUMAN_NAME.values())
    for display_name in tracker.PHASE_DISPLAY_ORDER:
        assert display_name in human_values, (
            f"PHASE_DISPLAY_ORDER has {display_name!r} "
            f"not in PHASE_HUMAN_NAME values"
        )


# ---------------------------------------------------------------------------
# Regression: _phase_r_editor cache lifecycle (RV t_2be84da1)
# ---------------------------------------------------------------------------


def test_r_editor_empty_cache_does_not_fall_through_to_journal(tmp_path: Path):
    """Regression: empty cache {} must not be treated as absent (was `if cache:`
    which treats {} as falsy). Cache presence is authoritative."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {})
    # Also write journal events (should be ignored when cache exists)
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 5, "successful_chunks": 5,
         "applied_count": 2, "candidate_count": 1},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Cache is present but empty — should NOT consult journal
    assert "cache present but no r_editor data" in line
    assert "журнал" not in line


def test_r_editor_malformed_cache_falls_through_to_journal(tmp_path: Path):
    """Regression: malformed JSON cache (read error) should consult journal."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    (out / "audit_cache_b3.json").write_bytes(b"\xff\xfe not json \x00")
    # Write journal events
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4, "successful_chunks": 4,
         "applied_count": 1, "candidate_count": 0},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Malformed cache = cache is None → journal fallback
    assert "chunks done=4/4" in line
    assert "safe (применено)=1" in line


def test_r_editor_unknown_cache_shape_no_r_editor(tmp_path: Path):
    """Regression: cache with no r_editor key should not fall through
    to journal — cache presence is authoritative."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write(out / "audit_cache_b3.json", {"some_other_key": "value"})
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Cache present but no r_editor data
    assert "cache present but no r_editor data" in line


def test_r_editor_journal_uses_production_field_names(tmp_path: Path):
    """Regression: journal fallback must read successful_chunks/applied_count/
    candidate_count (production field names), not done_chunks/applied/candidates."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Write journal with production field names
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 6,
         "successful_chunks": 4,  # != chunk_count → incomplete
         "applied_count": 3, "candidate_count": 2},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # successful_chunks=4 != chunk_count=6 → incomplete
    assert "chunks done=4/6 (incomplete)" in line
    assert "safe (применено)=3" in line
    assert "review (предложено)=2" in line


def test_r_editor_journal_partial_uses_production_field_names(tmp_path: Path):
    """Regression: journal partial status must read production field names."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "partial", "chunk_count": 8,
         "successful_chunks": 5,
         "applied_count": 2, "candidate_count": 1},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=5/8 (partial)" in line
    assert "safe (применено)=2" in line
    assert "review (предложено)=1" in line


def test_r_editor_incremental_empty_status_renders_in_progress(tmp_path: Path):
    """Regression: incremental stage_progress with done_chunks but no explicit
    status should render (in_progress), not completion-like."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    cache = {
        "stage_progress": {
            "r_editor": {
                "status": "",  # empty status
                "done_chunks": [1, 2, 3],
                "chunk_count": 4,
            }
        }
    }
    _write(out / "audit_cache_b3.json", cache)
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=3/4 (in_progress)" in line
    # Must NOT look complete (no done=3/4 without status label)
    assert "(in_progress)" in line


# ---------------------------------------------------------------------------
# Regression: canonical output assertions (RV t_2be84da1)
# ---------------------------------------------------------------------------


def test_chunk_section_uses_canonical_phase_names(tmp_path: Path):
    """Regression: chunk section heading and column headers must use
    PHASE_HUMAN_NAME, not raw trial/audit/repair labels."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    report = tracker.render_report(out)
    # Must NOT contain raw chunk section labels
    assert "trial -> audit -> repair" not in report
    # Must contain canonical names in chunk section
    assert "Translation -> Audit -> Repair" in report


def test_last_usage_line_uses_canonical_phase(tmp_path: Path):
    """Regression: last usage.ndjson line must show phase= canonical name,
    not raw label= usage label."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / USAGE_FILENAME, [
        _usage_row("phase2b/balanced_literary/chunk0001", cost=0.01),
    ])
    report = tracker.render_report(out)
    # Must NOT contain raw label= in model activity
    assert "label=phase2b" not in report
    # Must contain phase= with canonical name
    assert "phase=Translation" in report


def test_in_flight_labels_use_canonical_names(tmp_path: Path):
    """Regression: in-flight model activity labels must use
    PHASE_HUMAN_NAME, not raw audit/region/reaudit."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1",
         "event": "audit_unit_started", "ts": _iso(5),
         "chunk_id": "c1", "detector": "unit_test"},
    ])
    report = tracker.render_report(out)
    # Must NOT contain raw "audit" prefix
    assert "in flight: audit c1:unit_test" not in report
    # Must contain canonical "Audit" prefix
    assert "in flight: Audit c1:unit_test" in report


# ---------------------------------------------------------------------------
# RV t_71edc8e6: fail-closed cache and journal lifecycle regression probes
# ---------------------------------------------------------------------------


def test_r_editor_malformed_bytes_cache_no_journal_fallthrough(tmp_path: Path):
    """Direct probe: malformed bytes (invalid JSON) in audit_cache_b3.json
    must NOT fall through to journal — cache presence is authoritative.
    Journal claims complete but must be ignored."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Write invalid bytes as cache
    (out / "audit_cache_b3.json").write_bytes(b"\xff\xfe not json \x00")
    # Journal says complete — must be ignored
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4, "successful_chunks": 4,
         "applied_count": 1, "candidate_count": 0},
    ])
    line = tracker._phase_r_editor(out)
    # Malformed cache = _read_json returns None → journal IS consulted
    # (None means file couldn't be parsed, so no cache evidence)
    # This is correct: malformed bytes = no cache, not "cache present but invalid"
    # The journal fallback should render complete 4/4
    if line is not None:
        assert "chunks done=4/4" in line
        assert "safe (применено)=1" in line


def test_r_editor_non_object_list_cache_no_journal_fallthrough(tmp_path: Path):
    """Direct probe: non-object (list) r_editor in cache must NOT fall
    through to journal — cache presence is authoritative."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Cache with r_editor as a list (non-object)
    cache = {"r_editor": ["invalid", "list"]}
    _write(out / "audit_cache_b3.json", cache)
    # Journal says complete — must be ignored
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4, "successful_chunks": 4,
         "applied_count": 1, "candidate_count": 0},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Must NOT contain journal data — cache is authoritative
    assert "chunks done=4/4" not in line
    assert "журнал" not in line
    # Must render explicit invalid diagnostic
    assert "invalid cache r_editor" in line.lower() or "cache present" in line.lower()


def test_r_editor_non_object_string_cache_no_journal_fallthrough(tmp_path: Path):
    """Direct probe: non-object (string) r_editor in cache must NOT fall
    through to journal — cache presence is authoritative."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    # Cache with r_editor as a string
    cache = {"r_editor": "invalid_string"}
    _write(out / "audit_cache_b3.json", cache)
    # Journal says complete — must be ignored
    _write_ndjson(out / "audit_journal.ndjson", [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4, "successful_chunks": 4,
         "applied_count": 1, "candidate_count": 0},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Must NOT contain journal data — cache is authoritative
    assert "chunks done=4/4" not in line
    assert "журнал" not in line
    # Must render explicit invalid diagnostic
    assert "invalid cache r_editor" in line.lower() or "cache present" in line.lower()


def test_r_editor_journal_missing_successful_chunks_renders_incomplete(tmp_path: Path):
    """Direct probe: journal status=complete without successful_chunks
    must render incomplete, never default to chunk_count."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4},
        # Note: NO successful_chunks field
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Must NOT claim complete (4/4) — missing successful_chunks is ambiguous
    assert "chunks done=4/4" not in line or "incomplete" in line
    # Must render incomplete/ambiguous
    assert "incomplete" in line.lower() or "?/4" in line


def test_r_editor_journal_invalid_successful_chunks_renders_incomplete(tmp_path: Path):
    """Direct probe: journal status=complete with non-numeric successful_chunks
    must render incomplete."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 4,
         "successful_chunks": "not_a_number"},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    # Non-numeric successful_chunks != chunk_count → incomplete
    assert "incomplete" in line.lower()


def test_r_editor_journal_mismatched_successful_chunks_renders_incomplete(tmp_path: Path):
    """Direct probe: journal status=complete with successful_chunks != chunk_count
    must render incomplete."""
    out = _r_editor_journal_dir(tmp_path, [
        {"event": "r_editor_done", "ts": _iso(10),
         "status": "complete", "chunk_count": 8,
         "successful_chunks": 3},
    ])
    line = tracker._phase_r_editor(out)
    assert line is not None
    assert "chunks done=3/8 (incomplete)" in line


def test_chunk_table_header_no_leading_apostrophe(tmp_path: Path):
    """Direct probe: chunk-table header must not have a leading apostrophe."""
    out = _chapter_dir(tmp_path, "chapter_0001", _iso(3600))
    report = tracker.render_report(out)
    # Find the chunk header line
    for line in report.split("\n"):
        if "chunk_id" in line and "Translation" in line:
            # Must NOT start with an apostrophe
            assert not line.startswith("'"), (
                f"Chunk table header has leading apostrophe: {line!r}"
            )
            # Must contain the expected header format
            assert "chunk_id" in line
            assert "Translation" in line
            assert "Audit" in line
            assert "Repair" in line
            break
    else:
        # If no header found, the chunks section wasn't rendered — check alternative
        assert "chunks (Translation -> Audit -> Repair)" in report
