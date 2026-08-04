"""B7: book-run wrapper — sequential chapter runs with cross-chapter memory.

Runs chapters in order on a shared ``--memory-dir``, promoting observations
after each chapter based on its terminal status. The wrapper calls
``v4_phase12_strict_run`` for each chapter and ``MemoryManager.promote``
between chapters.

CLI::

    python -m pact_full_pipeline_runner_v1.v4_book_run \\
        --memory-dir <dir> --chapters 0001 0002 0003 \\
        --chapter-html-pattern 'chapters/{chapter_id}.html' \\
        --out-base <dir>

Artefacts: ``book_run.json`` in ``--out-base`` records the per-chapter
history (chapter_id, terminal status, promotion events, book_memory_hash
before/after).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pact_v4.phase1.memory import MemoryManager
from pact_v4.runtime.bible_renderer import render_bible_section

LOG = logging.getLogger(__name__)

BOOK_RUN_SCHEMA = "pact-v4-book-run/v1"


@dataclass
class BookRunRecord:
    chapter_id: str
    terminal_status: str
    book_memory_hash_before: str
    book_memory_hash_after: str
    promoted: bool
    promote_detail: str
    out_dir: str
    error: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "terminal_status": self.terminal_status,
            "book_memory_hash_before": self.book_memory_hash_before,
            "book_memory_hash_after": self.book_memory_hash_after,
            "promoted": self.promoted,
            "promote_detail": self.promote_detail,
            "out_dir": self.out_dir,
            "error": self.error,
        }


def _book_memory_hash(memory_dir: Path) -> str:
    import hashlib
    path = memory_dir / "book_memory.json"
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8")
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _run_one_chapter(
    chapter_id: str,
    *,
    memory_dir: Path,
    chapter_html_path: Path,
    out_dir: Path,
    extra_args: Sequence[str] = (),
) -> Dict[str, Any]:
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import main as strict_main

    argv = [
        "--chapter-id", chapter_id,
        "--chapter-html", str(chapter_html_path),
        "--memory-dir", str(memory_dir),
        "--out-dir", str(out_dir),
        *extra_args,
    ]
    try:
        result = strict_main(argv)
        return {"status": "ok", "record": result}
    except SystemExit as exc:
        return {"status": "exit", "code": exc.code}
    except Exception as exc:
        LOG.exception("Chapter %s failed", chapter_id)
        return {"status": "error", "error": str(exc)}


def _load_run_record(out_dir: Path) -> Dict[str, Any]:
    """Read ``strict_chapter_trial_record.json`` written by the strict driver.

    ``strict_main`` returns an int exit code, not the run record. The
    driver persists the full record at
    ``<out_dir>/strict_chapter_trial_record.json`` and ``<out_dir>/repair_report.json``
    — those are the source of truth for terminal status and quarantined
    chunks. Returns an empty dict on missing/corrupt files (the chapter
    failed before persisting).
    """
    record_path = out_dir / "strict_chapter_trial_record.json"
    if not record_path.exists():
        return {}
    try:
        return json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _quarantined_chunks_from_record(out_dir: Path) -> set:
    """Read quarantined chunk ids from the driver's selection_results.json.

    Quarantine status is recorded in the per-chunk selection record, not in
    the strict run record; this helper reads it directly.
    """
    selection_path = out_dir / "selection_results.json"
    if not selection_path.exists():
        return set()
    try:
        data = json.loads(selection_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    quarantined = set()
    for result in data.get("results", []):
        if result.get("status") == "quarantined":
            quarantined.add(result["chunk_id"])
    return quarantined


def run_book(
    *,
    memory_dir: Path,
    chapter_ids: List[str],
    chapter_html_pattern: str,
    out_base: Path,
    extra_args: Sequence[str] = (),
) -> Dict[str, Any]:
    memory_dir.mkdir(parents=True, exist_ok=True)
    out_base.mkdir(parents=True, exist_ok=True)
    manager = MemoryManager(str(memory_dir))

    records: List[BookRunRecord] = []
    for chapter_id in chapter_ids:
        chapter_html = Path(chapter_html_pattern.format(chapter_id=chapter_id))
        out_dir = out_base / f"chapter_{chapter_id}"
        hash_before = _book_memory_hash(memory_dir)

        result = _run_one_chapter(
            chapter_id,
            memory_dir=memory_dir,
            chapter_html_path=chapter_html,
            out_dir=out_dir,
            extra_args=extra_args,
        )

        terminal_status = "error"
        error_msg: Optional[str] = None
        run_record: Dict[str, Any] = {}
        if result["status"] == "ok":
            run_record = _load_run_record(out_dir)
            terminal_status = (
                run_record.get("step8", {}).get("status", "unknown")
            )
        elif result["status"] == "error":
            error_msg = result.get("error")

        quarantined = _quarantined_chunks_from_record(out_dir)
        promoted = False
        promote_detail = ""
        if terminal_status == "complete":
            # Invariant: complete does not filter observations; the
            # quarantine status of individual chunks is irrelevant when
            # the chapter as a whole reached complete. If this fires,
            # the chapter reports a contradictory state.
            assert not quarantined, (
                f"Chapter {chapter_id} terminal=complete but has "
                f"quarantined chunks: {sorted(quarantined)}"
            )
            manager.promote("complete")
            promoted = True
            promote_detail = "promoted after complete (all observations)"
        elif terminal_status == "accepted_degraded":
            manager.promote(
                "accepted_degraded",
                quarantined_chunks=quarantined,
            )
            promoted = True
            promote_detail = (
                f"promoted after accepted_degraded "
                f"(excluded {len(quarantined)} quarantined chunks)"
            )

        hash_after = _book_memory_hash(memory_dir)
        records.append(BookRunRecord(
            chapter_id=chapter_id,
            terminal_status=terminal_status,
            book_memory_hash_before=hash_before,
            book_memory_hash_after=hash_after,
            promoted=promoted,
            promote_detail=promote_detail,
            out_dir=str(out_dir),
            error=error_msg,
        ))

    book_run_path = out_base / "book_run.json"
    payload = {
        "schema": BOOK_RUN_SCHEMA,
        "memory_dir": str(memory_dir),
        "chapters": [rec.to_payload() for rec in records],
    }
    book_run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="V4 book-run wrapper (B7)")
    parser.add_argument("--memory-dir", required=True, type=Path)
    parser.add_argument("--chapters", nargs="+", required=True)
    parser.add_argument("--chapter-html-pattern", required=True,
                        help="Pattern with {chapter_id}, e.g. 'chapters/{chapter_id}.html'")
    parser.add_argument("--out-base", required=True, type=Path)
    args, extra = parser.parse_known_args(argv)
    result = run_book(
        memory_dir=args.memory_dir,
        chapter_ids=args.chapters,
        chapter_html_pattern=args.chapter_html_pattern,
        out_base=args.out_base,
        extra_args=extra,
    )
    failed = 0
    for rec in result["chapters"]:
        status = rec["terminal_status"]
        promoted = "promoted" if rec["promoted"] else "not promoted"
        marker = "" if status in ("complete", "accepted_degraded") else " [FAILED]"
        print(f"  {rec['chapter_id']}: {status} ({promoted}){marker}")
        if status not in ("complete", "accepted_degraded"):
            failed += 1
    if failed:
        print(
            f"\n{failed} chapter(s) did not reach complete/accepted_degraded. "
            "See book_run.json for details.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
