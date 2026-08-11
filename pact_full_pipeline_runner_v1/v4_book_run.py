"""B7: book-run wrapper — sequential chapter runs with cross-chapter memory.

Runs chapters in order on a shared ``--memory-dir``, promoting observations
after each chapter based on its terminal status. The wrapper calls
``v4_phase12_strict_run`` for each chapter and ``MemoryManager.promote``
between chapters.

B9 (owner decision 2026-08-04, V-final: deterministic source candidates
+ strict source->target evidence,
``docs/plans/V4_B9_GLOSSARY_OBSERVATIONS_TASK_RU.md``; see DECISIONS.md) adds
the glossary-candidate loop between the chapter run and ``promote``: after
each chapter the deterministic generator + consensus alignment
(``pact_v4.phase1.glossary_candidates``) produces candidate records from the
chapter source and ``out_dir/translations.json``, appends them to the
append-only ledger (``glossary_candidates.json``), and auto-promotes the
candidates that meet the v3 thresholds (proper_name >=
``--proper-name-min-occurrences``; term >= ``--term-min-chapters`` chapters
AND >= ``--term-min-occurrences`` total occurrences) with a single aligned
target. Promotion goes through the existing ``MemoryManager.add_observation``
-> ``promote`` path (B7), so the quarantined-chunk filter keeps working;
after ``promote`` the newly-promoted glossary entries are restored to the
flat ``{source: target}`` on-disk contract (``_glossary_entries`` skips dict
values). Only chapters that reached an accepted terminal result
(``complete`` / ``accepted_degraded``) contribute to the ledger and to
promotion — failed/unknown/errored chapters are excluded (review F1), and
candidate generation skips the B5 mixed-script allowlist (bible + glossary +
manual + source-derived, review F3). For ``accepted_degraded`` the
quarantined-chunk evidence is excluded BEFORE the ledger and auto-promotion
(B9-RV3): a candidate whose occurrences come only from quarantined chunks is
dropped entirely, and a mixed candidate keeps only its accepted-chunk
occurrences. When the PID->chunk plan backing that exclusion is missing,
corrupt, empty, ambiguous (duplicate PID/chunk ownership, malformed data) or
incomplete, the chapter fails closed (B9-F5/F6): no candidate, ledger,
observation or glossary contribution, with a logged warning (the run never
crashes). Zero model calls; identity/cache/journal untouched.

CLI::

    python -m pact_full_pipeline_runner_v1.v4_book_run \\
        --memory-dir <dir> --chapters 0001 0002 0003 \\
        --chapter-html-pattern 'chapters/{chapter_id}.html' \\
        --out-base <dir> [--candidates-ledger <path>]
        [--term-min-occurrences 3 --term-min-chapters 2]
        [--proper-name-min-occurrences 2 --consensus-ratio 0.8]
        [--bm-candidates-ledger <path>]
        [--bm-min-name-occurrences 2 --bm-min-name-chapters 2]

Artefacts: ``book_run.json`` in ``--out-base`` records the per-chapter
history (chapter_id, terminal status, promotion events, book_memory_hash
before/after, per-chapter ``candidates`` block ``{generated, proposed,
committed, conflicts}`` and per-chapter ``book_memory_candidates`` block of
the same shape plus ``book_memory_promotions`` promotion events with
evidence PIDs); ``glossary_candidates.json`` (default ``<out-base>``) is the
append-only glossary candidate ledger; ``book_memory_candidates.json``
(default ``<out-base>``) is the append-only book_memory candidate ledger.

Per-chapter ``candidates`` field semantics (exact definitions):

  * ``generated`` — number of aligned candidate records produced for this
    chapter (generation + consensus alignment, exclusions applied). Always 0
    for chapters that did not reach an accepted terminal result.
  * ``proposed`` — number of candidates from this chapter that were sent to
    ``MemoryManager.add_observation``: the aligned records that met the v3
    promotion thresholds (proper_name >= ``--proper-name-min-occurrences``
    with a single aligned target; term >= ``--term-min-chapters`` chapters
    AND >= ``--term-min-occurrences`` occurrences with a single aligned
    target), whose cumulative ledger record retains exactly one unambiguous
    target consistent with the chapter's aligned target, and that did not
    collide with an established glossary entry.
  * ``committed`` — how many of the ``proposed`` candidates actually landed
    in ``glossary.json`` after ``MemoryManager.promote``. Counted as the
    glossary key diff (before/after promote). For B9-generated observations
    ``committed == proposed`` for BOTH ``complete`` and ``accepted_degraded``
    (valid plan): quarantined pids are excluded BEFORE candidate generation
    (B9-RV3, with B9-F5/F6 fail-closed on unavailable or ambiguous PID->chunk
    provenance), so every proposed candidate carries an accepted ``chunk_id``
    that the B7 quarantined-chunk filter keeps. The B7 filter remains
    defense-in-depth: only independent (e.g. manual) observations carrying a
    quarantined ``chunk_id`` can be dropped, giving ``committed < proposed``
    for those; it never drops a B9-generated proposed candidate.
  * ``conflicts`` — aligned records that were NOT proposed because of an
    alignment conflict (several notable variants, no single target), a
    cumulative ledger target conflict (previous chapters resolved the source
    to a different target, so the merged record has no single target), or a
    conflict with an established glossary entry (different target).
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
from pact_v4.phase1.book_memory_candidates import (
    BookMemoryCandidateLedger,
    DEFAULT_MIN_NAME_CHAPTERS,
    DEFAULT_MIN_NAME_OCCURRENCES,
    generate_book_memory_candidates,
)
from pact_v4.phase1.glossary_candidates import (
    GlossaryCandidateLedger,
    _memory_terms,
    align_candidates,
    candidate_key,
    generate_candidates,
)
from pact_v4.phase1.memory import MemoryManager, atomic_write
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
    book_memory_candidates: Dict[str, int] = field(default_factory=dict)
    book_memory_promotions: List[Dict[str, Any]] = field(default_factory=list)
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
                "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
            },
            "book_memory_candidates": self.book_memory_candidates or {
                "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
            },
            "book_memory_promotions": self.book_memory_promotions,
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


def _pid_to_chunk(out_dir: Path) -> Optional[Dict[str, str]]:
    """``{pid: chunk_id}`` from the strict driver's ``chunk_plan.json``.

    Returns ``None`` — NOT an empty dict — when the plan is missing,
    corrupt, empty, or non-authoritative (B9-F6): duplicate PID
    ownership (a PID listed in more than one chunk, or twice within one
    chunk), malformed/non-list chunk or PID data, missing or duplicate
    chunk identity. The core ``ChunkPlanArtifact`` contract rejects such
    ownership, so the persisted plan is corrupt, not acceptable
    provenance, and must never be normalized or overwritten into a
    plausible-looking mapping. Callers filtering quarantined chunks MUST
    treat ``None`` as fail-closed (B9-F5/F6): an unavailable or
    ambiguous map cannot authoritatively exclude quarantined evidence
    and must not be read as "no quarantined chunks".
    """
    plan = _load_json(out_dir / "chunk_plan.json", None)
    if not isinstance(plan, dict):
        return None
    chunks = plan.get("chunks")
    if not isinstance(chunks, list):
        return None
    mapping: Dict[str, str] = {}
    seen_chunk_ids = set()
    for chunk in chunks:
        if not isinstance(chunk, dict):
            return None  # malformed chunk entry
        chunk_id = chunk.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            return None  # missing chunk identity
        if chunk_id in seen_chunk_ids:
            return None  # duplicate/ambiguous chunk identity
        seen_chunk_ids.add(chunk_id)
        pids = chunk.get("pids")
        if not isinstance(pids, list) or not pids:
            return None  # malformed/non-list/empty PID data
        for pid in pids:
            if not isinstance(pid, str) or not pid:
                return None  # malformed PID data
            if pid in mapping:
                return None  # duplicate PID ownership (within/across chunks)
            mapping[pid] = chunk_id
    if not mapping:
        return None  # empty plan — nothing authoritatively mapped
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

    B9-F5/F6 (fail closed on unavailable or ambiguous provenance): when
    ``excluded_chunk_ids`` is non-empty, the PID->chunk plan must first
    authoritatively exclude ALL quarantined evidence — a missing/corrupt/
    empty ``chunk_plan.json`` (``None`` mapping), a plan that does not map
    each source/translation pid exactly once to a valid chunk (duplicate
    PID ownership within/across chunks, duplicate or missing chunk
    identity, malformed/non-list chunk or PID data), or an incomplete plan
    (a source/translation pid the plan does not map) fails closed: the
    chapter generates no candidates, appends no ledger line, creates no
    observation and mutates no glossary. A warning is logged; the book run
    never crashes.

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
        if excluded:
            # B9-F5/F6 (fail closed on unavailable or ambiguous
            # quarantined-chunk provenance): an accepted_degraded chapter
            # with quarantined chunks may only generate candidates when the
            # PID->chunk plan authoritatively maps each source/translation
            # PID exactly once to a valid chunk. A missing/corrupt/empty/
            # ambiguous plan (``None`` — duplicate PID or chunk ownership,
            # malformed data, missing chunk identity) or an incomplete plan
            # (a source/translation pid the plan does not map) leaves pids
            # of unknown provenance — they could belong to a quarantined
            # chunk — so the chapter fails closed: no candidates, no ledger
            # line, no observation, no glossary mutation (a warning is
            # logged; the run never crashes).
            if pid_to_chunk is None:
                LOG.warning(
                    "B9-F6: %s accepted_degraded with quarantined chunks %s "
                    "but PID->chunk provenance missing/corrupt/empty/"
                    "ambiguous (duplicate or malformed ownership); failing "
                    "closed — no candidate generation, ledger line, "
                    "observation or glossary mutation",
                    out_dir.name, sorted(excluded),
                )
                return []
            plan_pids = {str(pid) for pid in pid_to_chunk}
            present_pids = (
                {str(pid) for pid in source_by_pid}
                | {str(pid) for pid in translations}
            )
            if not present_pids <= plan_pids:
                LOG.warning(
                    "B9-F5: %s accepted_degraded with quarantined chunks %s "
                    "but PID->chunk provenance incomplete (plan pids=%d, "
                    "unmapped source/translation pids=%s); failing closed — "
                    "no candidate generation, ledger line, observation or "
                    "glossary mutation",
                    out_dir.name, sorted(excluded), len(plan_pids),
                    sorted(present_pids - plan_pids),
                )
                return []
            # Quarantined chunks carry no authoritative evidence: drop their
            # pids from the source and the translation before generation and
            # alignment (B9-RV3). Every present pid is provably mapped by the
            # plan (checked above), so the remaining evidence is wholly from
            # accepted chunks.
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
            glossary=glossary,
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
    """B9: v3-threshold auto-promotion via ``MemoryManager.add_observation``.

    For every candidate aligned in THIS chapter, look up its cumulative
    ledger record (``merged_ledger``) and check the kind-specific thresholds
    (B9 card, v3 mechanics; owner decision 2026-08-04 — V-final):

      * ``proper_name``: ``total_occurrences >= proper_name_min_occurrences``
        and a single aligned target;
      * ``term``: ``len(chapters) >= term_min_chapters`` AND
        ``total_occurrences >= term_min_occurrences`` and a single aligned
        target.

    Promotion additionally requires the cumulative ledger record to retain
    exactly one unambiguous target consistent with the current chapter's
    aligned target (B9-F3, review finding): a record whose chapters resolved
    the source to DIFFERENT targets has ``target`` None forever (the merge is
    irreversible) and must be reported as a conflict and never proposed, even
    when this chapter alone is unambiguous.

    A candidate that passes is recorded as a glossary observation
    ``{target, type, chunk_id}`` (``chunk_id`` = first sorted chunk of the
    current chapter, so the existing B7 quarantined-chunk filter applies).
    Conflicts — competing alignment variants (no single target), a cumulative
    ledger target conflict (cross-chapter target disagreement), or an
    established glossary entry with a different target — are returned and
    NOT observed. Zero model calls; ``MemoryManager`` is untouched.

    Returns ``(proposed, conflicts)`` — the aligned records that were sent
    to ``add_observation`` (the book-run ``proposed`` count) and the aligned
    records that hit a conflict (the book-run ``conflicts`` count).
    """
    proposed: List[Dict[str, Any]] = []
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
        # B9-F3 (review finding, HIGH): the CUMULATIVE ledger record is the
        # authority on cross-chapter target agreement — the current chapter's
        # aligned record alone must never justify promotion. A record whose
        # chapters resolved the source to different targets has no single
        # merged target (``target`` is None forever — the merge is
        # irreversible, and every distinct chapter target sits in
        # ``targets_seen``/``conflicts``). Such a record is a conflict and
        # must never be proposed: proposing would persist an ambiguous
        # source->target mapping into glossary.json.
        record_target = record.get("target")
        if record_target is None:
            conflicts.append({
                **dict(aligned),
                "cumulative_targets": list(record.get("targets_seen") or []),
            })
            continue
        if str(record_target) != str(target):
            # Defensive: with the append-then-check ordering in run_book the
            # merged target of a single-distinct-target record always equals
            # the current chapter's aligned target; a mismatch means the
            # record disagrees with the current alignment and must not
            # promote.
            conflicts.append({
                **dict(aligned),
                "ledger_target": str(record_target),
            })
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
        proposed.append(dict(aligned))
    return proposed, conflicts


def _flatten_promoted_glossary(memory_dir: Path) -> None:
    """Restore the flat ``{source: target}`` glossary contract after promote.

    ``MemoryManager.promote`` stores observation values verbatim, so B9
    observations (``{target, type, chunk_id}``) land in ``glossary.json`` as
    dict values. The on-disk contract is flat (the existing 119 entries;
    ``_glossary_entries`` skips dict values), so any dict-valued entry
    carrying a string ``target`` is flattened back. ``book_memory.json`` is
    untouched (its entries are dicts by design).
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


# ---------------------------------------------------------------------------
# BM (V4.1 §15): book_memory candidate generation / ledger / auto-promotion
# ---------------------------------------------------------------------------


def _generate_book_memory_candidates_chapter(
    chapter_html: Path,
    out_dir: Path,
    memory_dir: Path,
    *,
    excluded_chunk_ids: Sequence[str] = (),
    mixed_script_allow: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """BM: deterministic book_memory candidates for one chapter (0 model calls).

    Source is the chapter source (parsed to pid blocks); exclusions come from
    the live ``book_memory.json``/``glossary.json`` in ``memory_dir`` and the
    B5 combined mixed-script allowlist (bible + glossary + manual +
    source-derived), exactly like the B9 glossary loop. Candidates are
    proper-name characters with gender only when the source explicitly
    confirms it (he/she/him/her in adjacent PIDs — fail-closed).

    Quarantined evidence (B9-RV3 pattern): for ``accepted_degraded`` with
    quarantined chunks, the pids owned by quarantined chunks are dropped from
    the source BEFORE generation — quarantined occurrences never count toward
    thresholds and never appear in candidate evidence. Missing/corrupt/empty/
    ambiguous ``chunk_plan.json`` (``_pid_to_chunk`` returns ``None``) or an
    incomplete plan (a source pid the plan does not map) fails closed
    (B9-F5/F6): the chapter generates no book_memory candidates.

    Degrades to ``[]`` on missing artifacts so the book run never crashes.
    """
    try:
        if not chapter_html.exists():
            return []
        source_by_pid = _source_by_pid(chapter_html)
        if not source_by_pid:
            return []
        pid_to_chunk = _pid_to_chunk(out_dir)
        excluded = {str(c) for c in (excluded_chunk_ids or ())}
        if excluded:
            if pid_to_chunk is None:
                LOG.warning(
                    "BM-F6: %s accepted_degraded with quarantined chunks %s "
                    "but PID->chunk provenance missing/corrupt/empty/"
                    "ambiguous; failing closed — no book_memory candidates",
                    out_dir.name, sorted(excluded),
                )
                return []
            plan_pids = {str(pid) for pid in pid_to_chunk}
            present_pids = {str(pid) for pid in source_by_pid}
            if not present_pids <= plan_pids:
                LOG.warning(
                    "BM-F5: %s accepted_degraded with quarantined chunks %s "
                    "but PID->chunk provenance incomplete (plan pids=%d, "
                    "unmapped source pids=%s); failing closed — no "
                    "book_memory candidates",
                    out_dir.name, sorted(excluded), len(plan_pids),
                    sorted(present_pids - plan_pids),
                )
                return []
            drop_pids = {
                pid for pid, chunk in pid_to_chunk.items()
                if str(chunk) in excluded
            }
            if drop_pids:
                source_by_pid = {
                    pid: text for pid, text in source_by_pid.items()
                    if pid not in drop_pids
                }
        if not source_by_pid:
            return []
        glossary = _load_json(memory_dir / "glossary.json", {})
        book_memory = _load_json(memory_dir / "book_memory.json", {})
        allowlist = combine_script_tokens(
            bible_script_tokens(book_memory),
            glossary_script_tokens(glossary),
            extract_script_tokens(" ".join(str(t) for t in mixed_script_allow)),
            source_derived_allowlist(
                " ".join(source_by_pid.values()),
                " ".join(str(t) for t in _load_json(
                    out_dir / "translations.json", {}
                ).values()),
            ),
        )
        return generate_book_memory_candidates(
            source_by_pid,
            book_memory=book_memory,
            glossary=glossary,
            allowlist=allowlist,
            pid_to_chunk=pid_to_chunk,
        )
    except Exception as exc:  # defensive: the candidate loop must not break a run
        LOG.warning("BM: candidate generation skipped for %s: %s",
                    out_dir.name, exc)
        return []


def _book_memory_has_name(book_memory: Mapping, name: str) -> bool:
    """Casefolded membership of ``name`` among book_memory character/entity
    names and their variants (established entries are never re-proposed)."""
    terms = {t.casefold() for t in _memory_terms(book_memory)}
    return name.casefold() in terms


def _auto_promote_book_memory(
    manager: MemoryManager,
    chapter_candidates: Sequence[Mapping[str, Any]],
    merged_ledger: Mapping[str, Mapping[str, Any]],
    book_memory: Mapping[str, Any],
    *,
    min_name_occurrences: int,
    min_name_chapters: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """BM: threshold auto-promotion via ``MemoryManager.add_observation``.

    For every character candidate aligned in THIS chapter, look up its
    cumulative ledger record and check the v3-style thresholds (spec §15:
    «имя ≥N раз / ≥M глав» — either branch suffices):

      * ``total_occurrences >= min_name_occurrences`` OR
      * ``len(chapters) >= min_name_chapters``.

    Promotion additionally requires the name NOT to be an established
    book_memory character/entity (defensive — generation excludes known
    names upstream, so this branch is reachable only through direct use).
    The promoted observation payload is section-scoped (``characters:<name>``)
    so ``promote`` merges it into ``book_memory.json["characters"]`` with the
    existing conflict resolution (established/locked never overwritten) and
    the quarantined-chunk filter (``chunk_id`` = first sorted chunk of the
    CURRENT chapter candidate — ``cand['chunk_ids']``, never a chunk from a
    prior chapter in the cumulative ledger, whose per-chapter chunk IDs
    repeat and could point at a chunk quarantined in this chapter). A
    candidate whose current-chapter accepted provenance is missing/invalid
    (empty ``chunk_ids``) fails closed: it is never promoted, and a prior
    chapter's chunk_id is never substituted. The cumulative ledger's chunk
    IDs are retained only as evidence/artifact (``cumulative_chunk_ids`` on
    the returned proposed records). ``gender`` is included ONLY when the
    cumulative ledger resolved a single source-confirmed gender
    (fail-closed: ambiguous or disagreeing chapters -> no gender field). A
    key-bound presence fact (``facts:<name>:presence``) and, when gender is
    confirmed, a gender fact (``facts:<name>:gender``) are observed
    alongside — the fact entry carries explicit ``keys`` so
    ``build_chapter_index`` can bind it.

    Returns ``(proposed, conflicts)`` — the candidate records sent to
    ``add_observation`` (book-run ``proposed`` count) and the records that hit
    an established-book_memory conflict (book-run ``conflicts`` count).
    Zero model calls; ``MemoryManager`` is untouched.
    """
    proposed: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for cand in chapter_candidates:
        source = str(cand.get("source") or "")
        kind = str(cand.get("kind") or "character")
        if not source:
            continue
        record = merged_ledger.get(candidate_key(source, kind))
        if not record:
            continue
        total = int(record.get("total_occurrences") or 0)
        chapters = [
            entry for entry in (record.get("chapters") or [])
            if isinstance(entry, dict) and entry.get("chapter_id")
        ]
        meets = (
            total >= min_name_occurrences
            or len(chapters) >= min_name_chapters
        )
        if not meets:
            continue
        if _book_memory_has_name(book_memory, source):
            conflicts.append({**dict(cand), "established": True})
            continue
        gender = record.get("gender")
        chapter_ids = sorted({str(e["chapter_id"]) for e in chapters})
        evidence_pids = sorted({
            str(p) for e in chapters for p in (e.get("evidence_pids") or [])
        })
        gender_evidence_pids = sorted({
            str(p) for e in chapters
            for p in (e.get("gender_evidence_pids") or [])
        })
        # RV fix (97571d3 finding): the observation's chunk_id — the B7
        # quarantined-chunk filter metadata — must come from the CURRENT
        # chapter candidate ONLY (``cand['chunk_ids']``), never from the
        # cumulative ledger. Per-chapter chunk IDs repeat (chunk0001, ...),
        # so the sorted union across cumulative chapters can pick a chunk
        # that is quarantined in THIS chapter, and the B7 filter would then
        # drop a valid promotion (proposed=1, committed=0). The cumulative
        # chunk IDs are kept only as evidence/artifact
        # (``cumulative_chunk_ids`` in the proposed record / book_run.json).
        current_chunk_ids = sorted({
            str(c) for c in (cand.get("chunk_ids") or [])
        })
        if not current_chunk_ids:
            # Fail-closed: current accepted provenance missing/invalid —
            # a prior chapter's chunk_id must never substitute for it.
            continue
        chunk_id = current_chunk_ids[0]
        cumulative_chunk_ids = sorted({
            str(c) for e in chapters for c in (e.get("chunk_ids") or [])
        })
        entry: Dict[str, Any] = {
            "type": "character",
            "chapters": chapter_ids,
            "variants": {},
            "forbidden_targets": [],
        }
        if gender:
            entry["gender"] = gender
        manager.add_observation(
            "book_memory", f"characters:{source}",
            {**entry, "chunk_id": chunk_id},
        )
        presence_fact: Dict[str, Any] = {
            "fact": (
                f"{source} appears in chapters "
                f"{', '.join(chapter_ids)}."
            ),
            "keys": [source],
            "source_pids": evidence_pids,
            "chapter": chapter_ids[-1] if chapter_ids else "",
        }
        manager.add_observation(
            "book_memory", f"facts:{source}:presence",
            {**presence_fact, "chunk_id": chunk_id},
        )
        if gender:
            gender_fact: Dict[str, Any] = {
                "fact": (
                    f"{source} is referred to with {gender} pronouns "
                    f"(he/him or she/her) in the source text."
                ),
                "keys": [source],
                "source_pids": gender_evidence_pids or evidence_pids,
                "chapter": chapter_ids[-1] if chapter_ids else "",
            }
            manager.add_observation(
                "book_memory", f"facts:{source}:gender",
                {**gender_fact, "chunk_id": chunk_id},
            )
        proposed.append({
            **dict(cand),
            "chapters": chapter_ids,
            "evidence_pids": evidence_pids,
            "gender": gender,
            "cumulative_chunk_ids": cumulative_chunk_ids,
        })
    return proposed, conflicts


def _strip_book_memory_observation_fields(memory_dir: Path) -> None:
    """Remove BM-internal fields (``chunk_id``) from promoted book_memory entries.

    ``MemoryManager.promote`` stores observation values verbatim, so the
    ``chunk_id`` carried for the quarantined-chunk filter would otherwise
    persist inside ``book_memory.json`` character/fact entries. Established
    entries never carry ``chunk_id`` (only BM observations do), so stripping
    it is a no-op for the existing bible and for chapters with no BM
    promotion (no write => bytes preserved).
    """
    path = memory_dir / "book_memory.json"
    if not path.exists():
        return
    data = _load_json(path, None)
    if not isinstance(data, dict):
        return
    changed = False
    for section in ("characters", "entities"):
        sec = data.get(section)
        if isinstance(sec, dict):
            for entry in sec.values():
                if isinstance(entry, dict) and "chunk_id" in entry:
                    entry.pop("chunk_id", None)
                    changed = True
    facts = data.get("facts")
    if isinstance(facts, list):
        for entry in facts:
            if isinstance(entry, dict) and "chunk_id" in entry:
                entry.pop("chunk_id", None)
                changed = True
    if changed:
        # RV fix (97571d3 finding #2): the authoritative book_memory.json
        # must be rewritten crash-safely — temp file in the same directory
        # + os.replace — never via a direct Path.write_text, which can leave
        # a torn/partial file if the process is interrupted mid-write.
        # atomic_write serializes identically (ensure_ascii=False, indent=2).
        atomic_write(str(path), data)


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
    bm_candidates_ledger: Optional[Path] = None,
    bm_min_name_occurrences: int = DEFAULT_MIN_NAME_OCCURRENCES,
    bm_min_name_chapters: int = DEFAULT_MIN_NAME_CHAPTERS,
) -> Dict[str, Any]:
    memory_dir.mkdir(parents=True, exist_ok=True)
    out_base.mkdir(parents=True, exist_ok=True)
    manager = MemoryManager(str(memory_dir))
    ledger = GlossaryCandidateLedger(
        str(candidates_ledger or (out_base / "glossary_candidates.json"))
    )
    bm_ledger = BookMemoryCandidateLedger(
        str(bm_candidates_ledger or (out_base / "book_memory_candidates.json"))
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

        # B9 (V-final, owner decision 2026-08-04): candidate generation +
        # consensus alignment -> ledger, then v3-threshold auto-promotion via
        # MemoryManager.add_observation, strictly after the chapter and
        # BEFORE MemoryManager.promote (GATE). Only chapters that reached an
        # accepted terminal result (complete / accepted_degraded) contribute
        # to the ledger or to promotion — failed/unknown/errored chapters
        # are excluded (review F1): their text was never accepted and must
        # not satisfy later thresholds or appear as promotion evidence.
        # Candidate generation skips the B5 mixed-script allowlist (bible +
        # glossary + manual + source-derived, review F3). For
        # accepted_degraded, quarantined-chunk evidence is excluded BEFORE
        # ledger accumulation and auto-promotion (B9-RV3) inside
        # _generate_and_align_chapter, with fail-closed on unavailable or
        # ambiguous PID->chunk provenance (B9-F5/F6). The actual glossary
        # write happens through the existing promote(status,
        # quarantined_chunks) below, so the B7 quarantined-chunk filter
        # applies to the proposed candidates.
        glossary_before = _load_json(memory_dir / "glossary.json", {})
        candidates_block = {
            "generated": 0,
            "proposed": 0,
            "committed": 0,
            "conflicts": 0,
        }
        proposed_sources: set = set()
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
            proposed_recs, conflict_recs = _auto_promote_glossary(
                manager,
                aligned,
                ledger.load(),
                glossary_before,
                term_min_chapters=term_min_chapters,
                term_min_occurrences=term_min_occurrences,
                proper_name_min_occurrences=proper_name_min_occurrences,
            )
            candidates_block["proposed"] = len(proposed_recs)
            candidates_block["conflicts"] = len(conflict_recs)
            proposed_sources = {
                str(p.get("source")) for p in proposed_recs if p.get("source")
            }

        # BM (V4.1 §15): deterministic book_memory candidate generation ->
        # ledger -> threshold auto-promotion via MemoryManager.add_observation,
        # strictly after the chapter and BEFORE MemoryManager.promote (GATE),
        # only for chapters with an accepted terminal result (complete /
        # accepted_degraded). Candidates are proper-name characters from the
        # chapter SOURCE with gender only when the source explicitly confirms
        # it (fail-closed); quarantined-chunk evidence is excluded BEFORE
        # generation (B9-RV3 pattern with BM-F5/F6 fail-closed on unavailable
        # or ambiguous PID->chunk provenance). The actual book_memory write
        # happens through the existing promote(status, quarantined_chunks)
        # below: section-scoped observations (characters:<name>,
        # facts:<name>:...) merge into book_memory.json with conflict
        # resolution (established/locked never overwritten) and the B7
        # quarantined-chunk filter.
        book_memory_before = _load_json(memory_dir / "book_memory.json", {})
        bm_candidates_block = {
            "generated": 0,
            "proposed": 0,
            "committed": 0,
            "conflicts": 0,
        }
        bm_promotions: List[Dict[str, Any]] = []
        if terminal_status in _PROMOTING_STATUSES:
            bm_candidates = _generate_book_memory_candidates_chapter(
                chapter_html,
                out_dir,
                memory_dir,
                excluded_chunk_ids=quarantined,
                mixed_script_allow=mixed_script_allow,
            )
            if bm_candidates:
                bm_ledger.append_chapter(chapter_id, bm_candidates)
            bm_candidates_block["generated"] = len(bm_candidates)
            bm_proposed_recs, bm_conflict_recs = _auto_promote_book_memory(
                manager,
                bm_candidates,
                bm_ledger.load(),
                book_memory_before,
                min_name_occurrences=bm_min_name_occurrences,
                min_name_chapters=bm_min_name_chapters,
            )
            bm_candidates_block["proposed"] = len(bm_proposed_recs)
            bm_candidates_block["conflicts"] = len(bm_conflict_recs)
            bm_promotions = [
                {
                    "source": str(p.get("source")),
                    "kind": str(p.get("kind") or "character"),
                    "gender": p.get("gender"),
                    "chapters": list(p.get("chapters") or []),
                    "evidence_pids": list(p.get("evidence_pids") or []),
                    "chunk_ids": sorted({
                        str(c) for c in (p.get("chunk_ids") or [])
                    }),
                    "cumulative_chunk_ids": sorted({
                        str(c) for c in (p.get("cumulative_chunk_ids") or [])
                    }),
                    "context": str(p.get("context") or ""),
                }
                for p in bm_proposed_recs
            ]

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

        # B9: promote stores observation values verbatim — restore the
        # flat {source: target} glossary contract for the promoted entries.
        _flatten_promoted_glossary(memory_dir)
        # BM: promote stores observation values verbatim — strip the
        # quarantined-filter-only chunk_id field from promoted book_memory
        # entries so the on-disk bible stays clean (no-op when nothing was
        # promoted; bytes preserved).
        _strip_book_memory_observation_fields(memory_dir)

        # B9: committed = how many of the proposed candidates actually
        # landed in glossary.json after promote. Counted as the glossary key
        # diff (before/after promote). For B9-generated observations
        # committed == proposed for BOTH complete and accepted_degraded
        # (valid plan): quarantined pids are excluded BEFORE generation
        # (B9-RV3, F5/F6 fail-closed), so every proposed candidate carries an
        # accepted chunk_id that the B7 filter keeps. The B7 filter is
        # defense-in-depth: only independent (e.g. manual) observations with
        # a quarantined chunk_id can be dropped (committed < proposed there);
        # it never drops a B9-generated proposed candidate.
        glossary_after = _load_json(memory_dir / "glossary.json", {})
        new_glossary_keys = set(glossary_after) - set(glossary_before)
        candidates_block["committed"] = len(proposed_sources & new_glossary_keys)

        # BM: committed = how many of the proposed character sources actually
        # landed in book_memory.json["characters"] after promote (key diff).
        # For BM-generated observations committed == proposed for BOTH
        # complete and accepted_degraded (valid plan): quarantined pids are
        # excluded BEFORE generation (BM-F5/F6 fail-closed), so proposed
        # observations carry accepted chunk_ids the B7 filter keeps; the
        # filter is defense-in-depth and can only lower committed for
        # independent observations carrying a quarantined chunk_id.
        book_memory_after = _load_json(memory_dir / "book_memory.json", {})
        bm_proposed_sources = {
            str(p.get("source")) for p in bm_promotions if p.get("source")
        }
        chars_before = set((book_memory_before.get("characters") or {}))
        chars_after = set((book_memory_after.get("characters") or {}))
        bm_candidates_block["committed"] = len(
            bm_proposed_sources & (chars_after - chars_before)
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
            candidates=candidates_block,
            book_memory_candidates=bm_candidates_block,
            book_memory_promotions=bm_promotions,
            error=error_msg,
        ))

    book_run_path = out_base / "book_run.json"
    payload = {
        "schema": BOOK_RUN_SCHEMA,
        "memory_dir": str(memory_dir),
        "candidates_ledger": str(ledger.path),
        "book_memory_candidates_ledger": str(bm_ledger.path),
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
    parser.add_argument(
        "--bm-candidates-ledger", type=Path, default=None,
        help="BM book_memory-candidate ledger path "
             "(default: <out-base>/book_memory_candidates.json)",
    )
    parser.add_argument("--bm-min-name-occurrences", type=int,
                        default=DEFAULT_MIN_NAME_OCCURRENCES,
                        help="BM character: min total occurrences before "
                             "promotion (v3-style, spec §15)")
    parser.add_argument("--bm-min-name-chapters", type=int,
                        default=DEFAULT_MIN_NAME_CHAPTERS,
                        help="BM character: min distinct chapters before "
                             "promotion (v3-style, spec §15)")
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
        bm_candidates_ledger=args.bm_candidates_ledger,
        bm_min_name_occurrences=args.bm_min_name_occurrences,
        bm_min_name_chapters=args.bm_min_name_chapters,
    )
    failed = 0
    for rec in result["chapters"]:
        status = rec["terminal_status"]
        promoted = "promoted" if rec["promoted"] else "not promoted"
        marker = "" if status in ("complete", "accepted_degraded") else " [FAILED]"
        cand = rec.get("candidates") or {}
        bm = rec.get("book_memory_candidates") or {}
        print(
            f"  {rec['chapter_id']}: {status} ({promoted}){marker}"
            f"  candidates: generated={cand.get('generated', 0)} "
            f"proposed={cand.get('proposed', 0)} "
            f"committed={cand.get('committed', 0)} "
            f"conflicts={cand.get('conflicts', 0)}"
            f"  bm: generated={bm.get('generated', 0)} "
            f"proposed={bm.get('proposed', 0)} "
            f"committed={bm.get('committed', 0)} "
            f"conflicts={bm.get('conflicts', 0)}"
        )
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
