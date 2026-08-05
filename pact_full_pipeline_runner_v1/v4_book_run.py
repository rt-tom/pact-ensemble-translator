"""B7: book-run wrapper — sequential chapter runs with cross-chapter memory.

Runs chapters in order on a shared ``--memory-dir``, promoting observations
after each chapter based on its terminal status. The wrapper calls
``v4_phase12_strict_run`` for each chapter and ``MemoryManager.promote``
between chapters.

B9-I2 (owner-approved 2026-08-04, ``docs/plans/V4_B9_GLOSSARY_OBSERVATIONS_TASK_RU.md``;
policy: Variant B auto-promotion — see DECISIONS.md) adds the glossary-candidate
loop between the chapter run and ``promote``: after each chapter the
deterministic generator + consensus alignment
(``pact_v4.phase1.glossary_candidates``) produces candidate records from the
chapter source and ``out_dir/translations.json``, appends them to the
append-only ledger (``glossary_candidates.json``), and auto-promotes the
candidates that meet the v3 thresholds through the existing
``MemoryManager.add_observation`` -> ``promote`` path (B7), so glossary growth
is automatic again (Variant B, owner decision 2026-08-04) but ONLY with strict
conservative source->target evidence: a single consensus target (consensus
0.8), no alignment conflicts, and unambiguous term targets (co-occurring
unrelated terms never share a target). Only chapters that reached an accepted
terminal result (``complete`` / ``accepted_degraded``) contribute to the ledger
— failed/unknown/errored chapters are excluded (B9-RV2 review F1), candidate
generation skips the B5 mixed-script allowlist (bible + glossary + manual +
source-derived, review F3), and for ``accepted_degraded`` the quarantined-chunk
evidence is excluded BEFORE the ledger and auto-promotion (B9-RV3): a candidate
whose occurrences come only from quarantined chunks is dropped entirely, and a
mixed candidate keeps only its accepted-chunk occurrences. The B7
``MemoryManager.promote`` path keeps the quarantined-chunk filter working;
after ``promote`` any dict-valued glossary entry is restored to the flat
``{source: target}`` on-disk contract (``_glossary_entries`` skips dict
values). Zero model calls; identity/cache/journal untouched.

CLI::

    python -m pact_full_pipeline_runner_v1.v4_book_run \\
        --memory-dir <dir> --chapters 0001 0002 0003 \\
        --chapter-html-pattern 'chapters/{chapter_id}.html' \\
        --out-base <dir> [--candidates-ledger <path>]
        [--term-min-occurrences 3 --term-min-chapters 2]
        [--proper-name-min-occurrences 2 --consensus-ratio 0.8]

Artefacts: ``book_run.json`` in ``--out-base`` records the per-chapter
history (chapter_id, terminal status, promotion events, book_memory_hash
before/after, per-chapter ``candidates`` block ``{generated, promoted,
conflicts}``); ``glossary_candidates.json`` (default ``<out-base>``) is the
append-only candidate ledger.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4._integrity_checks import (
    bible_script_tokens,
    combine_script_tokens,
    extract_script_tokens,
    glossary_script_tokens,
    source_derived_allowlist,
)
from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase1.glossary_candidates import (
    GlossaryCandidateLedger,
    align_candidates,
    candidate_key,
    generate_candidates,
)
from pact_v4.phase1.memory import MemoryManager
from pact_v4.runtime.bible_renderer import render_bible_section

LOG = logging.getLogger(__name__)

BOOK_RUN_SCHEMA = "pact-v4-book-run/v1"

# Terminal statuses after which ``MemoryManager.promote`` runs (B7).
_PROMOTING_STATUSES = ("complete", "accepted_degraded")


@dataclass
class BookRunRecord:
    chapter_id: str
    terminal_status: str
    book_memory_hash_before: str
    book_memory_hash_after: str
    promoted: bool
    promote_detail: str
    out_dir: str
    candidates: Dict[str, int] = field(default_factory=dict)
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
            "candidates": self.candidates or {
                "generated": 0, "promoted": 0, "conflicts": 0,
            },
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


# ---------------------------------------------------------------------------
# B9-I2: glossary-candidate generation / alignment / ledger / auto-promotion
# ---------------------------------------------------------------------------


def _load_json(path: Path, default: Any = None) -> Any:
    """Tolerant JSON loader (missing/corrupt file -> ``default``)."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _source_by_pid(chapter_html: Path) -> Dict[str, str]:
    """``{pid: text}`` for a chapter HTML file via the Phase 0B parser."""
    blocks = parse_source_html(chapter_html.read_text(encoding="utf-8-sig"))
    return {block.pid: block.text for block in blocks}


def _pid_to_chunk(out_dir: Path) -> Dict[str, str]:
    """``{pid: chunk_id}`` from the strict driver's ``chunk_plan.json``.

    Missing/corrupt plan -> empty mapping (candidates then carry no
    ``chunk_ids`` and are not filtered by quarantine).
    """
    plan = _load_json(out_dir / "chunk_plan.json", {})
    mapping: Dict[str, str] = {}
    if not isinstance(plan, dict):
        return mapping
    for chunk in plan.get("chunks", []) or []:
        if not isinstance(chunk, dict):
            continue
        chunk_id = chunk.get("chunk_id")
        if chunk_id is None:
            continue
        for pid in chunk.get("pids", []) or []:
            mapping[str(pid)] = str(chunk_id)
    return mapping


def _generate_and_align_chapter(
    chapter_html: Path,
    out_dir: Path,
    memory_dir: Path,
    *,
    proper_name_min_occurrences: int,
    term_min_occurrences: int,
    consensus_ratio: float,
    mixed_script_allow: Sequence[str] = (),
    excluded_chunk_ids: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """B9-I2: generate candidates + consensus-align targets for one chapter.

    Source is ``chapter_html`` (parsed to pid blocks), translation is
    ``out_dir/translations.json`` (``{pid: text}``). Exclusions come from the
    live ``glossary.json``/``book_memory.json`` in ``memory_dir`` and from the
    B5 combined mixed-script allowlist (review F3): bible + glossary + manual
    (``mixed_script_allow``, the ``deterministic_mixed_script_allow`` config
    override) + the chapter's source-derived allowlist (Latin tokens present
    in BOTH the source and the translation), exactly the union the strict
    runner builds for its mixed-script gate. ``pid_to_chunk`` comes from the
    driver's ``chunk_plan.json`` so candidates carry ``chunk_ids`` for the B7
    quarantined-chunk filter.

    B9-RV3 (quarantined evidence): when ``excluded_chunk_ids`` is non-empty
    (``accepted_degraded`` chapter with quarantined chunks), the pids that
    belong to those chunks are dropped from BOTH the source and the
    translation BEFORE candidate generation and alignment — quarantined
    occurrences never count toward occurrence thresholds, never appear in
    candidate ``chunk_ids``, and never shape the consensus target. A
    candidate whose evidence is wholly from quarantined chunks is therefore
    never generated at all: it has no ledger line and cannot promote.

    Degrades to ``[]`` on missing artifacts (e.g. a chapter that failed
    before persisting translations) so the book run never crashes on the
    candidate loop. No model calls, no HTTP.
    """
    try:
        if not chapter_html.exists():
            return []
        source_by_pid = _source_by_pid(chapter_html)
        translations = _load_json(out_dir / "translations.json", {})
        if not translations:
            return []
        pid_to_chunk = _pid_to_chunk(out_dir)
        excluded = {str(c) for c in (excluded_chunk_ids or ())}
        if excluded and pid_to_chunk:
            # Quarantined chunks carry no authoritative evidence: drop their
            # pids from the source and the translation before generation and
            # alignment (B9-RV3). Pids without a chunk mapping are kept — the
            # chunk plan is authoritative for what it covers.
            drop_pids = {
                pid for pid, chunk in pid_to_chunk.items()
                if str(chunk) in excluded
            }
            if drop_pids:
                source_by_pid = {
                    pid: text for pid, text in source_by_pid.items()
                    if pid not in drop_pids
                }
                translations = {
                    pid: text for pid, text in translations.items()
                    if pid not in drop_pids
                }
        if not source_by_pid or not translations:
            return []
        glossary = _load_json(memory_dir / "glossary.json", {})
        book_memory = _load_json(memory_dir / "book_memory.json", {})
        # B5 combined allowlist (V4_B5_MIXED_SCRIPT_POLICY_TASK_RU.md) —
        # bible + glossary + manual config + source-derived, tokenized the
        # same way the strict driver does, so an entry like "R.D.T."
        # contributes the tokens R/D/T that a candidate scan can match.
        allowlist = combine_script_tokens(
            bible_script_tokens(book_memory),
            glossary_script_tokens(glossary),
            extract_script_tokens(" ".join(str(t) for t in mixed_script_allow)),
            source_derived_allowlist(
                " ".join(source_by_pid.values()),
                " ".join(str(t) for t in translations.values()),
            ),
        )
        candidates = generate_candidates(
            source_by_pid,
            glossary=glossary,
            book_memory=book_memory,
            allowlist=allowlist,
            pid_to_chunk=pid_to_chunk,
            min_name_occurrences=proper_name_min_occurrences,
            min_term_occurrences=term_min_occurrences,
        )
        if not candidates:
            return []
        return align_candidates(
            candidates, source_by_pid, translations,
            consensus_ratio=consensus_ratio,
        )
    except Exception as exc:  # defensive: the candidate loop must not break a run
        LOG.warning("B9-I2: candidate generation skipped for %s: %s",
                    out_dir.name, exc)
        return []


def _flat_target(value: Any) -> Optional[str]:
    """Flat glossary target for an on-disk entry (str or ``{target: ...}``)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("target"), str):
        return value["target"]
    return None


def _auto_promote_glossary(
    manager: MemoryManager,
    chapter_aligned: Sequence[Mapping[str, Any]],
    merged_ledger: Mapping[str, Mapping[str, Any]],
    glossary: Mapping[str, Any],
    *,
    term_min_chapters: int,
    term_min_occurrences: int,
    proper_name_min_occurrences: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """B9-I2: v3-threshold auto-promotion via ``MemoryManager.add_observation``.

    For every candidate aligned in THIS chapter, look up its cumulative
    ledger record (``merged_ledger``) and check the kind-specific thresholds
    (B9-I2 card, v3 mechanics):

      * ``proper_name``: ``total_occurrences >= proper_name_min_occurrences``
        and a single aligned target;
      * ``term``: ``len(chapters) >= term_min_chapters`` AND
        ``total_occurrences >= term_min_occurrences`` and a single aligned
        target.

    Variant B (owner-approved 2026-08-04, DECISIONS.md): a candidate that
    passes is recorded as a glossary observation ``{target, type, chunk_id}``
    (``chunk_id`` = first sorted chunk of the current chapter, so the
    existing B7 quarantined-chunk filter applies). Only strict conservative
    evidence promotes: the alignment already rejected conflicting variants
    and ambiguous co-occurring term targets (``target=None``), so nothing
    unrelated reaches the glossary. Conflicts — competing alignment variants
    (no single target) or an established glossary entry with a different
    target — are returned and NOT observed. Zero model calls;
    ``MemoryManager`` is untouched.

    Returns ``(promoted, conflicts)`` — the aligned records that were
    observed for promotion and the aligned records that hit a conflict.
    """
    promoted: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for aligned in chapter_aligned:
        source = str(aligned.get("source") or "")
        kind = str(aligned.get("kind") or "term")
        if not source:
            continue
        if aligned.get("conflicts"):
            # Several notable variants — no single target (alignment conflict).
            conflicts.append(dict(aligned))
            continue
        target = aligned.get("target")
        if not target:
            continue
        record = merged_ledger.get(candidate_key(source, kind))
        if not record:
            continue
        total = int(record.get("total_occurrences") or 0)
        if kind == "proper_name":
            meets = total >= proper_name_min_occurrences
        else:
            chapters = [
                entry for entry in (record.get("chapters") or [])
                if isinstance(entry, dict) and entry.get("chapter_id")
            ]
            meets = (len(chapters) >= term_min_chapters
                     and total >= term_min_occurrences)
        if not meets:
            continue
        existing_target = _flat_target(glossary.get(source))
        if existing_target is not None and existing_target != target:
            conflicts.append({**dict(aligned), "established_target": existing_target})
            continue
        if existing_target == target:
            continue  # already established with the same target — no-op
        chunk_ids = sorted({str(c) for c in (aligned.get("chunk_ids") or [])})
        manager.add_observation("glossary", source, {
            "target": str(target),
            "type": kind,
            "chunk_id": chunk_ids[0] if chunk_ids else "",
        })
        promoted.append(dict(aligned))
    return promoted, conflicts


def _flatten_promoted_glossary(memory_dir: Path) -> None:
    """Restore the flat ``{source: target}`` glossary contract after promote.

    ``MemoryManager.promote`` stores observation values verbatim, so an
    observation written as ``{target, type, chunk_id}`` (the B9 candidate
    shape — a human promoting a candidate from the ledger by hand) lands in
    ``glossary.json`` as a dict value. The on-disk contract is flat (the
    existing 119 entries; ``_glossary_entries`` skips dict values), so any
    dict-valued entry carrying a string ``target`` is flattened back.
    ``book_memory.json`` is untouched (its entries are dicts by design).
    """
    path = memory_dir / "glossary.json"
    if not path.exists():
        return
    data = _load_json(path, None)
    if not isinstance(data, dict):
        return
    changed = False
    for source, value in data.items():
        if isinstance(value, dict) and isinstance(value.get("target"), str):
            data[source] = value["target"]
            changed = True
    if changed:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )


def run_book(
    *,
    memory_dir: Path,
    chapter_ids: List[str],
    chapter_html_pattern: str,
    out_base: Path,
    extra_args: Sequence[str] = (),
    candidates_ledger: Optional[Path] = None,
    term_min_occurrences: int = 3,
    term_min_chapters: int = 2,
    proper_name_min_occurrences: int = 2,
    consensus_ratio: float = 0.8,
    mixed_script_allow: Sequence[str] = (),
) -> Dict[str, Any]:
    memory_dir.mkdir(parents=True, exist_ok=True)
    out_base.mkdir(parents=True, exist_ok=True)
    manager = MemoryManager(str(memory_dir))
    ledger = GlossaryCandidateLedger(
        str(candidates_ledger or (out_base / "glossary_candidates.json"))
    )

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

        # B9-I2 (Variant B, owner-approved 2026-08-04): candidate generation
        # + consensus alignment -> ledger, then v3-threshold auto-promotion
        # via MemoryManager.add_observation -> promote (GATE: strictly after
        # the chapter and BEFORE the B7 promote below). Only chapters that
        # reached an accepted terminal result (complete / accepted_degraded)
        # contribute to the ledger — failed/unknown/errored chapters are
        # excluded (review F1): their text was never accepted and must not
        # satisfy later thresholds or appear as promotion evidence. For
        # accepted_degraded, quarantined-chunk evidence is excluded BEFORE
        # ledger accumulation and auto-promotion (B9-RV3): pids from
        # quarantined chunks never generate candidates, so a candidate whose
        # evidence is wholly quarantined has no ledger line and cannot
        # promote; mixed candidates only count their accepted-chunk
        # occurrences.
        candidates_block = {
            "generated": 0,
            "promoted": 0,
            "conflicts": 0,
        }
        if terminal_status in _PROMOTING_STATUSES:
            aligned = _generate_and_align_chapter(
                chapter_html,
                out_dir,
                memory_dir,
                proper_name_min_occurrences=proper_name_min_occurrences,
                term_min_occurrences=term_min_occurrences,
                consensus_ratio=consensus_ratio,
                mixed_script_allow=mixed_script_allow,
                excluded_chunk_ids=quarantined,
            )
            if aligned:
                ledger.append_chapter(chapter_id, aligned)
            candidates_block["generated"] = len(aligned)
            # Variant B: v3-threshold auto-promotion through the existing
            # add_observation -> promote path. Only strict conservative
            # evidence promotes (single consensus target, no alignment
            # conflict, unambiguous term target); everything else lands in
            # ``conflicts`` for the human reviewer.
            glossary_now = _load_json(memory_dir / "glossary.json", {})
            promoted_recs, conflict_recs = _auto_promote_glossary(
                manager,
                aligned,
                ledger.load(),
                glossary_now,
                term_min_chapters=term_min_chapters,
                term_min_occurrences=term_min_occurrences,
                proper_name_min_occurrences=proper_name_min_occurrences,
            )
            candidates_block["promoted"] = len(promoted_recs)
            candidates_block["conflicts"] = len(conflict_recs)

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

        # B9-I2: promote stores observation values verbatim — restore the
        # flat {source: target} glossary contract for the promoted entries.
        _flatten_promoted_glossary(memory_dir)

        hash_after = _book_memory_hash(memory_dir)
        records.append(BookRunRecord(
            chapter_id=chapter_id,
            terminal_status=terminal_status,
            book_memory_hash_before=hash_before,
            book_memory_hash_after=hash_after,
            promoted=promoted,
            promote_detail=promote_detail,
            out_dir=str(out_dir),
            candidates=candidates_block,
            error=error_msg,
        ))

    book_run_path = out_base / "book_run.json"
    payload = {
        "schema": BOOK_RUN_SCHEMA,
        "memory_dir": str(memory_dir),
        "candidates_ledger": str(ledger.path),
        "chapters": [rec.to_payload() for rec in records],
    }
    book_run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_argparser() -> argparse.ArgumentParser:
    """Build the book-run CLI parser (extracted for dry-run validation)."""
    parser = argparse.ArgumentParser(description="V4 book-run wrapper (B7/B9)")
    parser.add_argument("--memory-dir", required=True, type=Path)
    parser.add_argument("--chapters", nargs="+", required=True)
    parser.add_argument("--chapter-html-pattern", required=True,
                        help="Pattern with {chapter_id}, e.g. 'chapters/{chapter_id}.html'")
    parser.add_argument("--out-base", required=True, type=Path)
    parser.add_argument(
        "--candidates-ledger", type=Path, default=None,
        help="B9 glossary-candidate ledger path "
             "(default: <out-base>/glossary_candidates.json)",
    )
    parser.add_argument(
        "--mixed-script-allow", action="append", default=None,
        dest="mixed_script_allow",
        help="B5 manual mixed_script allowlist entry (repeatable; combined "
             "with bible/glossary/source-derived tokens for BOTH the B9 "
             "candidate exclusion and the strict driver's mixed-script gate "
             "— the same flag the strict run uses, so a book run configured "
             "with B5's manual allowlist excludes those tokens from the "
             "candidate ledger too)",
    )
    parser.add_argument("--term-min-occurrences", type=int, default=3,
                        help="term: min total occurrences across chapters (v3)")
    parser.add_argument("--term-min-chapters", type=int, default=2,
                        help="term: min distinct chapters before promotion (v3)")
    parser.add_argument("--proper-name-min-occurrences", type=int, default=2,
                        help="proper_name: min occurrences before promotion (v3)")
    parser.add_argument("--consensus-ratio", type=float, default=0.8,
                        help="dominant-variant share required for a target (0-1)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argparser()
    args, extra = parser.parse_known_args(argv)
    # B5 manual allowlist: consumed here for B9 candidate exclusion, and
    # re-forwarded to the strict driver so the real book-run allowlist is
    # exactly the same input (no divergent duplicate flag).
    for entry in (args.mixed_script_allow or ()):
        extra += ["--mixed-script-allow", str(entry)]
    result = run_book(
        memory_dir=args.memory_dir,
        chapter_ids=args.chapters,
        chapter_html_pattern=args.chapter_html_pattern,
        out_base=args.out_base,
        extra_args=extra,
        candidates_ledger=args.candidates_ledger,
        term_min_occurrences=args.term_min_occurrences,
        term_min_chapters=args.term_min_chapters,
        proper_name_min_occurrences=args.proper_name_min_occurrences,
        consensus_ratio=args.consensus_ratio,
        mixed_script_allow=args.mixed_script_allow or (),
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
