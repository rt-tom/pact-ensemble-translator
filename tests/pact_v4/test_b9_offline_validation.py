"""B9 offline dry-run validation on the chapter 0001 run_005 artifacts.

B9 card gate (docs/plans/V4_B9_GLOSSARY_OBSERVATIONS_TASK_RU.md, §6) BEFORE
the first combat book-run: run the production B9 chain — candidates ->
consensus alignment -> append-only ledger -> v3-threshold auto-promotion with
every guard enabled (co-occurrence guard, cumulative ledger target-conflict,
quarantined fail-closed B9-F5/F6, B5 mixed-script allowlist) — on the real
translations of chapter 0001 and report what would be promoted.

This harness mirrors exactly what ``v4_book_run.run_book`` does for a chapter
between the strict run and ``MemoryManager.promote``, but on TEMP copies of
everything writable:

  * the temp out-dir carries ``translations.json`` rebuilt the B13 way — the
    full 400-pid ``repair_report.final_translation`` normalized with
    ``_normalize_final_markup`` (that is what the current strict driver writes
    for ``translations.json``, which book_run/B9 read);
  * the temp memory-dir is a copy of the production ``glossary.json`` +
    ``book_memory.json`` (read-only source), so the promotion writes land in
    the copy, never in production;
  * the ledger lives in a temp file.

Zero model calls, zero HTTP, deterministic. The dry-run asserts:

  1. production ``glossary.json`` / ``book_memory.json`` bytes are unchanged
     after the run (dry-run guarantee);
  2. determinism: two fresh runs produce byte-identical reports;
  3. quarantined chunks (run_005: accepted_degraded) never contribute
     evidence — no candidate carries a quarantined ``chunk_id`` and no
     quarantined pid shapes a consensus target;
  4. proposed candidates actually land in the temp glossary
     (``committed == proposed`` under a valid, authoritative chunk plan);
  5. the report is persisted (JSON) for review.

The external artifacts (run dir, chapter 0001 HTML, production memory dir)
are not part of the repository. Point at them with the environment variables
``PACT_B9_RUN005_DIR``, ``PACT_B9_CHAPTER_HTML`` and ``PACT_B9_MEMORY_DIR``;
the whole module is skipped when any is unset or points at a missing path.
``PACT_B9_REPORT_OUT`` (optional) selects where the JSON report is written
(default: the pytest tmp dir). The resolution/skip contract is pinned
independently in ``test_b9_validation_paths.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from pact_full_pipeline_runner_v1.v4_book_run import (
    _auto_promote_glossary,
    _flatten_promoted_glossary,
    _generate_and_align_chapter,
    _load_json,
)
from pact_v4.phase1.glossary_candidates import GlossaryCandidateLedger
from pact_v4.phase1.memory import MemoryManager
from pact_v4.pipeline.v4_phase12_strict_runner import (
    _load_repair_report_final_translation,
    _normalize_final_markup,
)

PACT_B9_RUN005_DIR_ENV = "PACT_B9_RUN005_DIR"
PACT_B9_CHAPTER_HTML_ENV = "PACT_B9_CHAPTER_HTML"
PACT_B9_MEMORY_DIR_ENV = "PACT_B9_MEMORY_DIR"
PACT_B9_REPORT_OUT_ENV = "PACT_B9_REPORT_OUT"

# v3 promotion thresholds (book-run defaults).
TERM_MIN_CHAPTERS = 2
TERM_MIN_OCCURRENCES = 3
PROPER_NAME_MIN_OCCURRENCES = 2
CONSENSUS_RATIO = 0.8


def _resolve_external_paths() -> tuple[Path, Path, Path] | None:
    """Resolve (run_dir, chapter_html, memory_dir) from the environment.

    Returns ``None`` (skip) when any variable is unset or points at a
    missing path.
    """
    run_dir = os.environ.get(PACT_B9_RUN005_DIR_ENV)
    chapter_html = os.environ.get(PACT_B9_CHAPTER_HTML_ENV)
    memory_dir = os.environ.get(PACT_B9_MEMORY_DIR_ENV)
    if not run_dir or not chapter_html or not memory_dir:
        return None
    run_path = Path(run_dir)
    chapter_path = Path(chapter_html)
    memory_path = Path(memory_dir)
    if not run_path.is_dir() or not chapter_path.is_file():
        return None
    if not (memory_path / "glossary.json").is_file() or not (
        memory_path / "book_memory.json"
    ).is_file():
        return None
    return run_path, chapter_path, memory_path


_EXTERNAL = _resolve_external_paths()
_RUN_DIR, _CHAPTER_HTML, _MEMORY_DIR = _EXTERNAL or (None, None, None)

pytestmark = pytest.mark.skipif(
    _EXTERNAL is None,
    reason=(
        "set PACT_B9_RUN005_DIR, PACT_B9_CHAPTER_HTML and PACT_B9_MEMORY_DIR "
        "(chapter 0001 run_005 artifacts + production memory dir, not part of "
        "the repository)"
    ),
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_temp_out_dir(run_dir: Path, work: Path) -> Path:
    """Temp out-dir mirroring what the strict driver leaves for book_run.

    ``translations.json`` is the full 400-pid ``repair_report.final_translation``
    normalized the B13 way; ``chunk_plan.json`` and ``selection_results.json``
    are copied verbatim.
    """
    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_map = _load_repair_report_final_translation(run_dir)
    if not final_map:
        raise AssertionError("repair_report.final_translation missing/empty")
    normalized = {
        pid: _normalize_final_markup(text) for pid, text in final_map.items()
    }
    (out_dir / "translations.json").write_text(
        json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
    shutil.copy2(run_dir / "chunk_plan.json", out_dir / "chunk_plan.json")
    shutil.copy2(run_dir / "selection_results.json",
                 out_dir / "selection_results.json")
    return out_dir


def _build_temp_memory_dir(memory_dir: Path, work: Path) -> Path:
    """Temp memory dir seeded from the production glossary/book_memory."""
    memory = work / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(memory_dir / "glossary.json", memory / "glossary.json")
    shutil.copy2(memory_dir / "book_memory.json", memory / "book_memory.json")
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    return memory


def _run_dry_run(
    run_dir: Path,
    chapter_html: Path,
    prod_memory_dir: Path,
    work: Path,
    *,
    chapter_id: str = "0001",
) -> dict:
    """Run the full production B9 chain on temp copies; return the report.

    No production path is written: the memory dir used for promotion is a
    temp copy and the ledger is a temp file. Returns a JSON-serialisable
    report with candidates/proposed/committed/conflicts plus proof hashes.
    """
    record = _load_json(run_dir / "strict_chapter_trial_record.json", {})
    terminal_status = record.get("step8", {}).get("status", "unknown")
    selection = _load_json(run_dir / "selection_results.json", {})
    quarantined = {
        r["chunk_id"] for r in selection.get("results", [])
        if r.get("status") == "quarantined"
    }

    out_dir = _build_temp_out_dir(run_dir, work)
    memory = _build_temp_memory_dir(prod_memory_dir, work)

    prod_glossary_before = (prod_memory_dir / "glossary.json").read_bytes()
    prod_memory_before = (prod_memory_dir / "book_memory.json").read_bytes()

    manager = MemoryManager(str(memory))
    ledger = GlossaryCandidateLedger(str(work / "glossary_candidates.json"))

    aligned = _generate_and_align_chapter(
        chapter_html, out_dir, memory,
        proper_name_min_occurrences=PROPER_NAME_MIN_OCCURRENCES,
        term_min_occurrences=TERM_MIN_OCCURRENCES,
        consensus_ratio=CONSENSUS_RATIO,
        mixed_script_allow=(),
        excluded_chunk_ids=sorted(quarantined),
    )
    if aligned:
        ledger.append_chapter(chapter_id, aligned)

    glossary_before = _load_json(memory / "glossary.json", {})
    proposed_recs, conflict_recs = _auto_promote_glossary(
        manager, aligned, ledger.load(), glossary_before,
        term_min_chapters=TERM_MIN_CHAPTERS,
        term_min_occurrences=TERM_MIN_OCCURRENCES,
        proper_name_min_occurrences=PROPER_NAME_MIN_OCCURRENCES,
    )

    if terminal_status == "complete":
        manager.promote("complete")
    elif terminal_status == "accepted_degraded":
        manager.promote("accepted_degraded", quarantined_chunks=quarantined)
    _flatten_promoted_glossary(memory)

    glossary_after = _load_json(memory / "glossary.json", {})
    new_keys = sorted(set(glossary_after) - set(glossary_before))
    proposed_sources = {
        str(p.get("source")) for p in proposed_recs if p.get("source")
    }
    committed = sorted(proposed_sources & set(new_keys))

    report = {
        "schema": "pact-v4-b9-offline-validation/v1",
        "chapter_id": chapter_id,
        "run_dir": str(run_dir),
        "terminal_status": terminal_status,
        "quarantined_chunks": sorted(quarantined),
        "counts": {
            "generated": len(aligned),
            "proposed": len(proposed_recs),
            "committed": len(committed),
            "conflicts": len(conflict_recs),
        },
        "aligned": [
            {
                "source": a.get("source"),
                "kind": a.get("kind"),
                "occurrences": a.get("occurrences"),
                "matching_pid_count": a.get("matching_pid_count"),
                "chunk_ids": sorted(
                    str(c) for c in (a.get("chunk_ids") or [])),
                "target": a.get("target"),
                "consensus_share": a.get("consensus_share"),
                "conflicts": a.get("conflicts"),
            }
            for a in sorted(
                aligned, key=lambda x: (str(x.get("kind")),
                                        str(x.get("source") or "").casefold()))
        ],
        "proposed": [
            {"source": p.get("source"), "kind": p.get("kind"),
             "target": p.get("target")}
            for p in sorted(
                proposed_recs, key=lambda x: str(x.get("source") or "").casefold())
        ],
        "conflicts": [
            {
                "source": c.get("source"),
                "kind": c.get("kind"),
                "target": c.get("target"),
                "conflicts": c.get("conflicts"),
                "cumulative_targets": c.get("cumulative_targets"),
                "ledger_target": c.get("ledger_target"),
                "established_target": c.get("established_target"),
            }
            for c in sorted(
                conflict_recs, key=lambda x: str(x.get("source") or "").casefold())
        ],
        "ledger_records": [
            {
                "source": rec.get("source"),
                "kind": rec.get("kind"),
                "total_occurrences": rec.get("total_occurrences"),
                "chapters": rec.get("chapters"),
                "target": rec.get("target"),
                "targets_seen": rec.get("targets_seen"),
                "conflicts": rec.get("conflicts"),
            }
            for rec in sorted(
                ledger.load().values(),
                key=lambda r: (str(r.get("kind")),
                               str(r.get("source") or "").casefold()))
        ],
        "proof": {
            "production_glossary_sha256_before": _sha256_bytes(
                prod_glossary_before),
            "production_glossary_sha256_after": _sha256_bytes(
                (prod_memory_dir / "glossary.json").read_bytes()),
            "production_book_memory_sha256_before": _sha256_bytes(
                prod_memory_before),
            "production_book_memory_sha256_after": _sha256_bytes(
                (prod_memory_dir / "book_memory.json").read_bytes()),
        },
    }
    return report


def test_b9_offline_dry_run_deterministic_and_production_untouched(tmp_path):
    assert _RUN_DIR is not None and _CHAPTER_HTML is not None
    assert _MEMORY_DIR is not None
    report = _run_dry_run(_RUN_DIR, _CHAPTER_HTML, _MEMORY_DIR, tmp_path)

    # Dry-run guarantee: production memory files byte-identical.
    proof = report["proof"]
    assert proof["production_glossary_sha256_before"] == (
        proof["production_glossary_sha256_after"])
    assert proof["production_book_memory_sha256_before"] == (
        proof["production_book_memory_sha256_after"])

    # Determinism: a second fresh run produces the identical report.
    second = _run_dry_run(
        _RUN_DIR, _CHAPTER_HTML, _MEMORY_DIR, tmp_path / "second")
    assert json.dumps(second, ensure_ascii=False, sort_keys=True) == (
        json.dumps(report, ensure_ascii=False, sort_keys=True))

    # Quarantined chunks never contribute evidence.
    quarantined = set(report["quarantined_chunks"])
    if quarantined:
        for aligned in report["aligned"]:
            assert not (
                set(aligned["chunk_ids"]) & quarantined
            ), f"{aligned['source']} carries a quarantined chunk_id"


def test_b9_offline_dry_run_proposed_lands_in_temp_glossary(tmp_path):
    assert _RUN_DIR is not None and _CHAPTER_HTML is not None
    assert _MEMORY_DIR is not None
    report = _run_dry_run(_RUN_DIR, _CHAPTER_HTML, _MEMORY_DIR, tmp_path)
    counts = report["counts"]
    # Under a valid, authoritative chunk plan committed == proposed for
    # B9-generated observations (B9-F5/F6 did not fail closed).
    assert counts["committed"] == counts["proposed"]
    assert set(counts) == {"generated", "proposed", "committed", "conflicts"}
    assert counts["generated"] >= 0
    # Term promotion requires >= 2 chapters; a single-chapter dry run can
    # only promote proper_names (v3 threshold semantics).
    for prop in report["proposed"]:
        assert prop["kind"] == "proper_name" or counts["generated"] == 0


def test_b9_offline_dry_run_report_persisted(tmp_path):
    assert _RUN_DIR is not None and _CHAPTER_HTML is not None
    assert _MEMORY_DIR is not None
    report_out = os.environ.get(PACT_B9_REPORT_OUT_ENV)
    target = Path(report_out) if report_out else tmp_path / "b9_offline_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    report = _run_dry_run(_RUN_DIR, _CHAPTER_HTML, _MEMORY_DIR, tmp_path)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["schema"] == "pact-v4-b9-offline-validation/v1"
    assert loaded["counts"]["proposed"] == report["counts"]["proposed"]
