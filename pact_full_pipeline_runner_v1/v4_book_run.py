"""B7: book-run wrapper — sequential chapter runs with cross-chapter memory.

Runs chapters in order on a shared ``--memory-dir``, promoting observations
after each chapter based on its terminal status. The wrapper calls
``v4_phase12_strict_run`` for each chapter and ``MemoryManager.promote``
between chapters.

GLOSSARY-FROM-ENTITY (owner decision 2026-08-15, variant B) replaces the
deterministic B9 glossary scan: after each accepted chapter
(``complete`` / ``accepted_degraded``) the run reads the entity extractor's
VALIDATED context (``entity_context_cache.json``, written by the strict run
BEFORE generation — 0 extra model calls), promotes the VERIFIED claims into
book_memory (SAFE-MEMORY), and derives glossary candidates from the verified
proper-noun entities. The existing consensus alignment
(``pact_v4.phase1.glossary_candidates.align_candidates``) extracts the
ACTUAL Russian target from the chapter translation — the glossary entry is
exactly what the model wrote (consistent with the text). The aligned target
also fills ``canonical_ru`` in the book_memory entity observations. The
deterministic B9 scan (``generate_candidates`` + v3-threshold
auto-promotion) is removed from the run path (its helpers remain as dead
code for reference, like the B7 book_memory_candidates script).

Only chapters that reached an accepted terminal result contribute to
promotion — failed/unknown/errored chapters are excluded (review F1), and
promotion goes through the existing ``MemoryManager.add_observation`` ->
``promote`` path (B7), so the quarantined-chunk filter keeps working; after
``promote`` the newly-promoted glossary entries are restored to the flat
``{source: target}`` on-disk contract (``_glossary_entries`` skips dict
values). Zero model calls in the promotion loop; identity/cache/journal
untouched.

CLI::

    python -m pact_full_pipeline_runner_v1.v4_book_run \\
        --memory-dir <dir> --chapters 0001 0002 0003 \\
        --chapter-html-pattern 'chapters/{chapter_id}.html' \\
        --out-base <dir> [--candidates-ledger <path>]
        [--consensus-ratio 0.8]

Artefacts: ``book_run.json`` in ``--out-base`` records the per-chapter
history (chapter_id, terminal status, promotion events, book_memory_hash
before/after, per-chapter ``candidates`` block ``{generated, proposed,
committed, conflicts}`` and per-chapter ``book_memory_candidates`` block of
the same shape plus ``book_memory_promotions`` promotion events with
evidence PIDs); ``glossary_candidates.json`` (default ``<out-base>``) is the
append-only glossary candidate ledger; ``book_memory_candidates.json``
(default ``<out-base>``) is the append-only book_memory candidate ledger.

Per-chapter ``candidates`` field semantics (exact definitions):

  * ``generated`` — number of entity-derived glossary candidate records
    produced for this chapter (proper-noun verified entities, proposed +
    conflicts). Always 0 for chapters that did not reach an accepted
    terminal result and for chapters without a validated entity context.
  * ``proposed`` — number of candidates from this chapter that were sent to
    ``MemoryManager.add_observation``: an aligned record with a single
    target that did not collide with an established glossary entry.
  * ``committed`` — how many of the ``proposed`` candidates actually landed
    in ``glossary.json`` after ``MemoryManager.promote``. Counted as the
    glossary key diff (before/after promote). For ``complete`` chapters
    ``committed == proposed``. For ``accepted_degraded`` the B7
    quarantined-chunk filter is defense-in-depth: an entity observation
    whose anchor pid's chunk is quarantined is dropped (``committed <
    proposed``) — quarantined evidence never locks a glossary entry.
  * ``conflicts`` — aligned records that were NOT proposed because of an
    alignment conflict (several notable variants, no single target), a
    multi-word entity name without an established ``canonical_ru``, or a
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

# v41 italics: formatting model-call defaults (mirror V3 Defaults["formatting"])
# v41 fix: dynamic max_tokens via resolve_format_mappings (40*spans+500, min 800 cap 8192)
# _DEFAULT_FORMATTING_CFG max_tokens is None sentinel (dynamic) — effective budget
# computed per-batch in resolve_format_mappings. Explicit int overrides dynamic.
_DEFAULT_FORMATTING_CFG: dict = {
    "enabled": True,
    "required": False,
    "temperature": 0.1,
    "top_p": 0.9,
    "top_k": 32,
    "enable_thinking": False,
    "max_tokens": None,
    "generation_retries": 2,
    "tags": ["em", "strong", "i", "b", "a"],
    "required_tags": ["em", "strong", "i", "b", "a"],
    "optional_tags": [],
    "max_blocks_per_call": 12,
    "retry_unresolved_spans": True,
    "on_failure": "omit_tag",
    "formatting_single_call_whole_chapter": True,
}
# lenient default: do not block chapter on unresolved italics (debt)
_DEFAULT_MAX_FORMATTING_INCIDENTS = 999

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
    index_built: bool = False
    error: Optional[str] = None
    media_confirmation: Optional[Dict[str, Any]] = None
    media_error: Optional[str] = None

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
            "index_built": self.index_built,
            "error": self.error,
            "media_confirmation": self.media_confirmation,
            "media_error": self.media_error,
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


def _quarantined_pids_for_book_run(out_dir: Path, quarantined_chunks: set) -> set:
    """Map quarantined chunk IDs → PID set via pid_to_chunk (evidence PID exclusion).

    Evidence PIDs are quarantined when their owning chunk is quarantined.
    Uses _pid_to_chunk (chunk_plan.json) for authoritative mapping; if the
    plan is missing/ambiguous (None), returns empty set (fail-closed at
    call site: no authoritative exclusion, but promotion still validated
    via allowed_evidence_pids and in-memory B3 filtering).
    """
    if not quarantined_chunks:
        return set()
    pid_to_chunk = _pid_to_chunk(out_dir)
    if pid_to_chunk is None:
        return set()
    return {pid for pid, chunk in pid_to_chunk.items() if chunk in quarantined_chunks}


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


def _next_chapter_id(chapter_id: str, chapter_ids: Sequence[str]) -> Optional[str]:
    """The chapter that runs right after ``chapter_id`` in this book run.

    ``chapter_ids`` is the run's ordered chapter list; the NEXT chapter's
    index entry is pre-built when the current one is accepted (RV finding
    1, SAFE-MEMORY 2026-08-14) so its FIRST run already sees the memory of
    accepted chapters < it. Returns ``None`` when ``chapter_id`` is the
    last chapter (or absent from the list).
    """
    try:
        idx = list(chapter_ids).index(chapter_id)
    except ValueError:
        return None
    if idx + 1 >= len(chapter_ids):
        return None
    return str(chapter_ids[idx + 1])


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
    """B9: proper-name auto-promotion via ``MemoryManager.add_observation``.

    P0 owner decision 2026-08-14: ONLY ``proper_name`` candidates
    auto-promote (threshold: ``proper_name_min_occurrences``, default 2).
    Generic ``term`` candidates are NEVER promoted by frequency/stability
    (door → дверь is stable but not terminology; a real term may translate
    unstably early on) — they remain ledger observations only.

    For every proper-name candidate aligned in THIS chapter, look up its
    cumulative ledger record (``merged_ledger``) and check the kind-specific
    threshold (B9 card, v3 mechanics; owner decision 2026-08-04 — V-final):

      * ``proper_name``: ``total_occurrences >= proper_name_min_occurrences``
        and a single aligned target;

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
        # P0 owner decision 2026-08-14 (term auto-promotion OFF): only
        # proper_name auto-promotes (threshold: 2 occurrences). Generic
        # terms are NEVER promoted by frequency/stability — door → дверь is
        # stable but not terminology, and a real term may translate
        # unstably early on (Other → другой/иной/Иной). Generic terms stay
        # in the observation ledger only (never in the prompt).
        if kind != "proper_name":
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


def _detect_execution_host() -> str:
    """Trusted execution host: where this process runs (media vs rt).

    Uses explicit env PACT_V4_HOST / PACT_EXEC_HOST first, then platform.
    This is the trusted signal the launcher threads to the transport decision
    so RT never silently uses the local facade even if its Linux path exists.
    """
    import os as _os
    import sys as _sys
    env = _os.environ.get("PACT_V4_HOST") or _os.environ.get("PACT_EXEC_HOST")
    if env in ("rt", "media"):
        return env
    if _sys.platform == "win32":
        return "rt"
    return "media"


def _flag_value(extra, flag):
    for i, val in enumerate(extra):
        if val == flag and i + 1 < len(extra):
            return str(extra[i + 1])
        if isinstance(val, str) and val.startswith(flag + "="):
            return val.split("=", 1)[1]
    return None


class _FormattingBackendClient:
    """Adapter wrapping a CompletionBackend for resolve_format_mappings."""
    def __init__(self, backend, runtime=None):
        self._backend = backend
        self._runtime = runtime
    def complete(self, messages, cfg, max_tokens, label=None):
        from pact_v4.runtime.backend_protocol import CompletionRequest, Message
        msgs = tuple(Message(role=str(m.get("role", "user")), content=str(m.get("content", ""))) for m in messages)
        model_ref = "default"
        try:
            from pact_v4.runtime.backend_role_adapters import _model_ref_for
            model_ref = _model_ref_for(self._backend, ("generator", "default"))
        except Exception:
            try:
                bindings = getattr(getattr(self._backend, "descriptor", None), "model_bindings", {}) or {}
                model_ref = bindings.get("generator") or bindings.get("default") or "default"
            except Exception:
                model_ref = "default"
        # v41: formatting reasoning 0 — never pass reasoning in request_options
        req = CompletionRequest(
            model_ref=model_ref,
            messages=msgs,
            max_output_tokens=int(max_tokens),
            temperature=float(cfg.get("temperature", 0.1)),
            response_schema={"type": "json_object"},
            label=label or "formatting",
        )
        resp = self._backend.complete(req)
        # Propagate reasoning/finish_reason/usage for diagnostics (v41 3.3)
        # CompletionResponse has finish_reason/usage; reasoning may be in raw_metadata
        reasoning = ""
        try:
            raw_md = getattr(resp, "raw_metadata", {}) or {}
            if isinstance(raw_md, dict):
                reasoning = str(raw_md.get("reasoning") or raw_md.get("reasoning_content") or "")
        except Exception:
            reasoning = ""
        finish_reason = str(getattr(resp, "finish_reason", "") or "")
        usage = dict(getattr(resp, "usage", {}) or {})
        text = resp.text if hasattr(resp, "text") else str(resp)
        # v41 round2 fix: propagate backend's actual record, not request intent.
        # raw_metadata["response_format_attempted"] is False when ApiClient fell back
        # after grammar rejection; using req.response_schema would incorrectly report True.
        try:
            _raw_md2 = getattr(resp, "raw_metadata", None) or {}
            if isinstance(_raw_md2, dict) and "response_format_attempted" in _raw_md2:
                _val = _raw_md2["response_format_attempted"]
                if _val is None:
                    response_format_attempted = bool(getattr(req, "response_schema", None))
                else:
                    response_format_attempted = bool(_val)
            else:
                response_format_attempted = bool(getattr(req, "response_schema", None))
        except Exception:
            response_format_attempted = bool(getattr(req, "response_schema", None))
        class _Gen:
            def __init__(self, content, finish_reason, usage, reasoning, response_format_attempted):
                self.content = content
                self.text = content
                self.finish_reason = finish_reason
                self.usage = usage
                self.reasoning = reasoning
                self.reasoning_content = reasoning
                self.response_format_attempted = response_format_attempted
        return _Gen(text, finish_reason, usage, reasoning, response_format_attempted)
    def close(self):
        if self._runtime is not None:
            try:
                self._runtime.close()
            except Exception:
                pass


def _formatting_backend_with_overrides(backend):
    """Apply formatting-specific overrides: reasoning 0 and external->managed for opencode.

    For Local: force gemma reasoning 0. For OpenCode: force reasoning 0 and when
    server_mode is external, promote to managed (ManagedServerProcess / OpenCodeServerProcess)
    on port from base_url (e.g. 4097) so formatting can start its own server per-chapter (proposal D2).
    For Composite: apply recursively to each sub-backend.
    """
    try:
        from dataclasses import replace as _replace
        from pact_v4.runtime.runtime_config import CompositeBackendConfig as _Composite, LocalLlamaBackendConfig as _Local, OpenCodeBackendConfig as _OpenCode
        from pact_v4.runtime.opencode_server_lifecycle import ManagedServerSpec as _Spec
        from pact_v4.runtime.runtime_config import _parse_url_port as _port_of
        from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _gemma_server_args_for_reasoning as _gemma_args0
        if isinstance(backend, _Local):
            new_sa = dict(backend.server_args)
            new_sa["gemma"] = _gemma_args0(0)
            return _replace(backend, server_args=new_sa)
        if isinstance(backend, _OpenCode):
            new_server = backend.server
            # reasoning 0 override
            if getattr(new_server, "reasoning", None) not in (None, 0):
                new_server = _replace(new_server, reasoning=0)
            if backend.server_mode == "external":
                port = _port_of(new_server.base_url) or 4096
                # Use ManagedServerSpec with port from base_url (handles 4097 for remote)
                spec = _Spec(port=port, hostname="127.0.0.1")
                # Also handle explicit managed spec if backend already has one
                try:
                    if backend.managed is not None:
                        spec = _replace(backend.managed, port=port)
                except Exception:
                    pass
                # ManagedServerProcess will be started by build_runtime;
                # Need to ensure server_mode managed and managed spec set.
                return _replace(backend, server=new_server, server_mode="managed", managed=spec)
            if new_server is not backend.server:
                return _replace(backend, server=new_server)
            return backend
        if isinstance(backend, _Composite):
            new_backends = {}
            for _name, _sub in backend.backends.items():
                if isinstance(_sub, _Local):
                    _nsa = dict(_sub.server_args)
                    _nsa["gemma"] = _gemma_args0(0)
                    new_backends[_name] = _replace(_sub, server_args=_nsa)
                elif isinstance(_sub, _OpenCode):
                    new_backends[_name] = _formatting_backend_with_overrides(_sub)
                else:
                    new_backends[_name] = _sub
            return _replace(backend, backends=new_backends)
    except Exception:
        pass
    return backend


def _build_formatting_client(args, extra, fmt_cfg, out_dir=None):
    if not fmt_cfg.get("enabled", True):
        return None
    rc_path = _flag_value(extra, "--runtime-config")
    translator = _flag_value(extra, "--translator")
    reviewer = _flag_value(extra, "--reviewer")
    providers_cfg = _flag_value(extra, "--providers-config")
    if rc_path is None and hasattr(args, "runtime_config") and getattr(args, "runtime_config", None):
        rc_path = str(getattr(args, "runtime_config"))
    if translator is None and hasattr(args, "translator") and getattr(args, "translator", None):
        translator = str(getattr(args, "translator"))
    if reviewer is None and hasattr(args, "reviewer") and getattr(args, "reviewer", None):
        reviewer = str(getattr(args, "reviewer"))
    if providers_cfg is None and hasattr(args, "providers_config") and getattr(args, "providers_config", None):
        providers_cfg = str(getattr(args, "providers_config"))
    try:
        backend = None
        runtime = None
        if rc_path:
            from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_runtime_config_file
            backend = _load_runtime_config_file(Path(rc_path))
            if translator or reviewer:
                from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _apply_provider_flags as _apf
                class _Tmp:
                    pass
                tmp = _Tmp()
                tmp.translator = translator
                tmp.reviewer = reviewer
                tmp.providers_config = Path(providers_cfg) if providers_cfg else None
                backend = _apf(tmp, backend)
            backend = _formatting_backend_with_overrides(backend)
        else:
            # Historical local default — same backend as strict-runner run_local_default
            # (required so the ordinary CLI path without --runtime-config still resolves
            # formatting via the generator role instead of falling back to debt).
            from pact_full_pipeline_runner_v1.v4_phase12_strict_run import GEMMA_PATH, QWEN_PATH, QWEN_SERVER_ARGS, _gemma_server_args_for_reasoning
            from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig
            backend = LocalLlamaBackendConfig(
                exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
                device="SYCL0",
                host="127.0.0.1",
                model_paths={"gemma": GEMMA_PATH, "qwen": QWEN_PATH},
                model_names={"gemma": GEMMA_PATH.name, "qwen": QWEN_PATH.name},
                server_args={"gemma": _gemma_server_args_for_reasoning(0), "qwen": list(QWEN_SERVER_ARGS)},
                port=8094,
            )
            if translator or reviewer:
                try:
                    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _apply_provider_flags as _apf2
                    class _Tmp2:
                        pass
                    tmp2 = _Tmp2()
                    tmp2.translator = translator
                    tmp2.reviewer = reviewer
                    tmp2.providers_config = Path(providers_cfg) if providers_cfg else None
                    backend = _apf2(tmp2, backend)
                except Exception:
                    pass
            backend = _formatting_backend_with_overrides(backend)
        if backend is None:
            return None
        if out_dir is not None:
            log_dir = Path(out_dir) / "server_logs"
        else:
            log_dir = Path(getattr(args, "memory_dir", Path("/tmp"))) / "server_logs_fmt" if hasattr(args, "memory_dir") else Path("/tmp")
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        runtime = None
        try:
            runtime = backend.build_runtime(log_dir=log_dir)
            from pact_v4.runtime.runtime_config import build_role_backend
            fmt_backend = build_role_backend(backend, runtime)
            return _FormattingBackendClient(fmt_backend, runtime)
        except Exception as exc:
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:
                    pass
            LOG.warning("formatting client build failed (%s) — falling back to debt", exc)
            return None
    except Exception as exc:
        LOG.warning("formatting client build failed (%s) — falling back to debt", exc)
        return None


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
    promote_existing_dir: Optional[Path] = None,
    media_book_id: Optional[str] = None,
    media_transport: Optional[Any] = None,
    media_root: str = "/home/rt/pact_runs",
    media_target: str = "media",
    media_max_retries: int = 1,
    media_exec_host: Optional[str] = None,
    formatting_cfg: Optional[Mapping[str, Any]] = None,
    formatting_client: Optional[Any] = None,
    max_formatting_incidents: int = _DEFAULT_MAX_FORMATTING_INCIDENTS,
    glossary_resolver_mode: str = "off",
    glossary_resolver_cache_miss_policy: str = "recompute",
) -> Dict[str, Any]:
    # Media sync pre-init hook: fetch authoritative state before MemoryManager init
    if media_book_id is not None:
        from pact_v4.snapshot.run_hooks import pre_init_fetch
        _exec_host = media_exec_host if media_exec_host is not None else _detect_execution_host()
        pre_init_fetch(media_book_id, memory_dir, transport=media_transport, ssh_target=media_target, root=media_root, execution_host=_exec_host)
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
        # B1 (promote-only, owner 2026-08-19): when --promote-existing is set,
        # the chapter's strict trial is REUSED from an already-completed
        # out_dir instead of re-running the strict pipeline (the translator
        # model, e.g. Muse, may no longer be available). Only the acceptance +
        # promotion stage runs (entity/glossary/book_memory/index) — never the
        # strict generation/audit/repair. Everything consumed below (terminal
        # status, quarantined set, entity_context_cache, translations.json) is
        # read from the SAME --promote-existing dir, so promotion is
        # byte-identical to what book_run would have done had the strict run
        # succeeded.
        if promote_existing_dir is not None:
            out_dir = promote_existing_dir
            run_record_pre = _load_run_record(out_dir)
            if not run_record_pre:
                result = {
                    "status": "error",
                    "error": (
                        f"--promote-existing: no strict_chapter_trial_record.json "
                        f"in {out_dir}"
                    ),
                }
            else:
                # Mark "ok" so the shared acceptance/promotion block below
                # runs with the TUNED terminal status from the existing record.
                result = {"status": "ok"}
        else:
            out_dir = out_base / f"chapter_{chapter_id}"
            result = _run_one_chapter(
                chapter_id,
                memory_dir=memory_dir,
                chapter_html_path=chapter_html,
                out_dir=out_dir,
                extra_args=extra_args,
            )
        hash_before = _book_memory_hash(memory_dir)

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
        quarantined_pids = _quarantined_pids_for_book_run(out_dir, quarantined)

        # v41 italics: formatting restoration (per-chapter B7/B9). After
        # finalization (post-repair/edit) the plain Russian translation is
        # wrapped with <em> via a targeted model-call (resolve_format_mappings)
        # + deterministic wrap (run_formatting_align). Plain translation stays
        # plain until this point (phase2/prompts.py untouched). Lenient debt
        # policy: unresolved spans become FormattingIncident debt, chapter does
        # not fail. Source html is available via chapter_html_pattern.
        if terminal_status in _PROMOTING_STATUSES:
            try:
                fmt_cfg = dict(_DEFAULT_FORMATTING_CFG)
                if formatting_cfg:
                    fmt_cfg.update(dict(formatting_cfg))
                # formatting disabled or no inline spans -> no model call, still write report via wrap path
                if fmt_cfg.get("enabled", True) and chapter_html.exists():
                    _trans_path = out_dir / "translations.json"
                    _translations = _load_json(_trans_path, {})
                    if _translations:
                        try:
                            from pact_v4.phase5.formatting import (
                                FORMATTING_POLICY_VERSION,
                                resolve_format_mappings,
                                run_formatting_align,
                            )
                        except Exception:
                            raise
                        _blocks = parse_source_html(chapter_html.read_text(encoding="utf-8-sig"))
                        has_spans = any(b.inline_spans for b in _blocks)
                        if has_spans:
                            # Resolve target_text via model (only PIDs with spans)
                            # Per-chapter formatting lifecycle (book-formatting-remote-server):
                            # build backend per chapter when has_spans, health-wait, then close in finally.
                            # Injected formatting_client (tests) takes precedence; otherwise build from extra_args/out_dir.
                            _mappings: dict = {}
                            _per_chapter_fmt_client = None
                            _fmt_health_error: Optional[str] = None
                            if formatting_client is not None:
                                try:
                                    _mappings = resolve_format_mappings(formatting_client, fmt_cfg, _blocks, _translations, out_dir=out_dir)
                                except Exception as _fmt_exc:
                                    _fmt_health_error = str(_fmt_exc)
                                    LOG.warning("v41 formatting resolve failed for %s (injected client): %s", chapter_id, _fmt_exc)
                                    _mappings = {}
                            else:
                                # Snapshot pre-existing logs to isolate strict-run logs from formatting lifecycle.
                                # Collision-proof: snapshot identity (mtime/size/inode), not just filename, so
                                # a formatting log that overwrites a strict log with same second-resolution name
                                # is still detected as new and strict content is not silently lost (unique stamp
                                # in lifecycle prevents overwrite, metadata check is safety net).
                                _log_dir_fmt = out_dir / "server_logs"
                                try:
                                    _log_dir_fmt.mkdir(parents=True, exist_ok=True)
                                except Exception:
                                    pass
                                try:
                                    _pre_existing_log_meta: dict[str, tuple] = {}
                                    for _p in _log_dir_fmt.glob("opencode_serve_*.log"):
                                        try:
                                            _st = _p.stat()
                                            _pre_existing_log_meta[_p.name] = (_st.st_mtime_ns, _st.st_size, getattr(_st, "st_ino", None))
                                        except Exception:
                                            _pre_existing_log_meta[_p.name] = None  # type: ignore
                                except Exception:
                                    _pre_existing_log_meta = {}
                                _per_chapter_fmt_client = None
                                try:
                                    # Build per-chapter formatting backend (remote -> managed on same port)
                                    # ManagedServerProcess / OpenCodeServerProcess health-wait on GET /global/health (port from base_url -> 4097)
                                    _ns = type("FmtArgs", (), {"memory_dir": memory_dir, "runtime_config": None, "translator": None, "reviewer": None, "providers_config": None})()
                                    _per_chapter_fmt_client = _build_formatting_client(_ns, list(extra_args), fmt_cfg, out_dir=out_dir)
                                    if _per_chapter_fmt_client is not None:
                                        _mappings = resolve_format_mappings(_per_chapter_fmt_client, fmt_cfg, _blocks, _translations, out_dir=out_dir)
                                    else:
                                        _fmt_health_error = "formatting backend unavailable: GET /global/health failed (port 4097)"
                                        LOG.warning("formatting backend unavailable for %s — falling back to lenient debt (GET /global/health failed)", chapter_id)
                                        _mappings = {}
                                except Exception as _fmt_exc:
                                    # Health failure / connection refused -> lenient debt without crashing
                                    _fmt_health_error = str(_fmt_exc)
                                    LOG.warning("formatting server health failed for %s (GET /global/health, error: %s) — falling back to lenient debt", chapter_id, _fmt_exc)
                                    _mappings = {}
                                finally:
                                    # Preserve only logs created by formatting lifecycle (snapshot isolation)
                                    try:
                                        if _per_chapter_fmt_client is not None:
                                            try:
                                                _per_chapter_fmt_client.close()
                                            except Exception:
                                                pass
                                        # Only consider new logs created during formatting lifecycle
                                        try:
                                            import shutil
                                            _all_logs = list(_log_dir_fmt.glob("opencode_serve_*.log"))
                                            _new_logs = []
                                            for _p in _all_logs:
                                                if "opencode_serve_fmt_" in _p.name:
                                                    continue
                                                if _p.name not in _pre_existing_log_meta:
                                                    _new_logs.append(_p)
                                                else:
                                                    _prev = _pre_existing_log_meta.get(_p.name)
                                                    if _prev is None:
                                                        # Prior stat unavailable -> conservatively not new (avoid strict leak)
                                                        continue
                                                    try:
                                                        _cur_st = _p.stat()
                                                        _cur = (_cur_st.st_mtime_ns, _cur_st.st_size, getattr(_cur_st, "st_ino", None))
                                                        if _cur != _prev:
                                                            _new_logs.append(_p)
                                                    except Exception:
                                                        _new_logs.append(_p)
                                            for _p in _new_logs:
                                                _target = _p.parent / _p.name.replace("opencode_serve_", "opencode_serve_fmt_", 1)
                                                if not _target.exists():
                                                    try:
                                                        shutil.copy(str(_p), str(_target))
                                                    except Exception:
                                                        try:
                                                            _target.write_text(_p.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
                                                        except Exception:
                                                            pass
                                            # Startup failure before logs exist (e.g., port-occupancy preflight):
                                            # ensure diagnostic fmt log exists with real error, not synthetic empty placeholder.
                                            if _fmt_health_error is not None:
                                                _fmt_existing = list(_log_dir_fmt.glob("opencode_serve_fmt_*.log"))
                                                if not _fmt_existing:
                                                    try:
                                                        _err_text_for_log = _fmt_health_error
                                                        if "/global/health" not in _err_text_for_log:
                                                            _err_text_for_log = f"GET /global/health failed: {_err_text_for_log}"
                                                        _diag_log = _log_dir_fmt / f"opencode_serve_fmt_{chapter_id}_health.log"
                                                        _diag_log.write_text(
                                                            f"formatting startup failed for {chapter_id}: {_err_text_for_log}\n",
                                                            encoding="utf-8",
                                                        )
                                                    except Exception:
                                                        pass
                                        except Exception:
                                            pass
                                        # Write required per-chapter failure metadata before lenient fallback (finding 2)
                                        if _fmt_health_error is not None:
                                            _meta_path = out_dir / "formatting_batch1_meta.json"
                                            if not _meta_path.exists():
                                                try:
                                                    from pact_v4.phase5.formatting import _effective_max_tokens
                                                    _span_count = sum(len(b.inline_spans) for b in _blocks)
                                                    _cfg_max_raw = fmt_cfg.get("max_tokens")
                                                    try:
                                                        _cfg_max_int = int(_cfg_max_raw) if _cfg_max_raw is not None else None
                                                    except Exception:
                                                        _cfg_max_int = None
                                                    _effective = _effective_max_tokens(_span_count, _cfg_max_int)
                                                    _err_text = _fmt_health_error
                                                    if "/global/health" not in _err_text:
                                                        _err_text = f"GET /global/health failed: {_err_text}"
                                                    _meta = {
                                                        "batch": 1,
                                                        "attempt": 1,
                                                        "span_count": _span_count,
                                                        "effective_max_tokens": _effective,
                                                        "finish_reason": None,
                                                        "usage": None,
                                                        "response_format_attempted": None,
                                                        "error": _err_text,
                                                    }
                                                    _meta_path.write_text(json.dumps(_meta, ensure_ascii=False, indent=2), encoding="utf-8")
                                                    _attempt_meta = out_dir / "formatting_batch1_attempt1_meta.json"
                                                    if not _attempt_meta.exists():
                                                        _attempt_meta.write_text(json.dumps(_meta, ensure_ascii=False, indent=2), encoding="utf-8")
                                                except Exception:
                                                    pass
                                    except Exception:
                                        pass
                            # Injected-client health failure also needs diagnostic meta (finding 2)
                            if _fmt_health_error is not None and not (out_dir / "formatting_batch1_meta.json").exists():
                                try:
                                    from pact_v4.phase5.formatting import _effective_max_tokens as _eff2
                                    _sc2 = sum(len(b.inline_spans) for b in _blocks)
                                    _cfg2 = fmt_cfg.get("max_tokens")
                                    try:
                                        _cfg2_i = int(_cfg2) if _cfg2 is not None else None
                                    except Exception:
                                        _cfg2_i = None
                                    _eff_val2 = _eff2(_sc2, _cfg2_i)
                                    _err2 = _fmt_health_error
                                    if "/global/health" not in _err2:
                                        _err2 = f"GET /global/health failed: {_err2}"
                                    _meta2 = {"batch": 1, "attempt": 1, "span_count": _sc2, "effective_max_tokens": _eff_val2, "finish_reason": None, "usage": None, "response_format_attempted": None, "error": _err2}
                                    (out_dir / "formatting_batch1_meta.json").write_text(json.dumps(_meta2, ensure_ascii=False, indent=2), encoding="utf-8")
                                    _am2 = out_dir / "formatting_batch1_attempt1_meta.json"
                                    if not _am2.exists():
                                        _am2.write_text(json.dumps(_meta2, ensure_ascii=False, indent=2), encoding="utf-8")
                                except Exception:
                                    pass
                            # Determine backend hash for report (from run_record lifecycle if available)
                            _backend_hash = ""
                            try:
                                _backend_hash = str((run_record.get("identities") or {}).get("backend_identity_hash") or run_record.get("backend_identity_hash") or "")
                            except Exception:
                                _backend_hash = ""
                            if not _backend_hash:
                                import hashlib as _hashlib
                                _backend_hash = _hashlib.sha256(chapter_id.encode("utf-8")).hexdigest()[:16]
                            _outcome = run_formatting_align(
                                blocks=_blocks,
                                translation=_translations,
                                backend_identity_hash=_backend_hash,
                                policy_version=FORMATTING_POLICY_VERSION,
                                max_formatting_incidents=int(max_formatting_incidents),
                                mappings=_mappings,
                            )
                            # Overwrite translations.json with formatted text (with <em>)
                            _formatted_map = dict(_outcome.formatted_text)
                            atomic_write(str(_trans_path), _formatted_map)
                            # Write formatting_report.json next to translations.json
                            (_trans_path.parent / "formatting_report.json").write_text(
                                json.dumps(_outcome.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
                            )
                        else:
                            # No inline spans in chapter -> ensure formatting_report exists (empty)
                            try:
                                from pact_v4.phase5.formatting import FORMATTING_POLICY_VERSION as _FPV, run_formatting_align as _RFA
                                _empty_blocks: list = []
                                _backend_hash2 = ""
                                try:
                                    _backend_hash2 = str((run_record.get("identities") or {}).get("backend_identity_hash") or "")
                                except Exception:
                                    pass
                                if not _backend_hash2:
                                    import hashlib as _hashlib2
                                    _backend_hash2 = _hashlib2.sha256(chapter_id.encode("utf-8")).hexdigest()[:16]
                                _out2 = _RFA(blocks=_empty_blocks, translation=_translations, backend_identity_hash=_backend_hash2, policy_version=_FPV, max_formatting_incidents=int(max_formatting_incidents), mappings={})
                                (_trans_path.parent / "formatting_report.json").write_text(json.dumps(_out2.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
                            except Exception:
                                pass
            except Exception as exc:  # noqa: BLE001 -- formatting debt, never break a run
                LOG.warning("v41 formatting step skipped for %s: %s", chapter_id, exc)

        # GLOSSARY-FROM-ENTITY (owner decision 2026-08-15, variant B): the
        # deterministic B9 scan (generate_candidates + v3-threshold
        # auto-promotion, 248 garbage candidates/chapter) is REMOVED from the
        # book run. Glossary candidates now come from the source-only entity
        # extractor's VERIFIED entities (entity_context_cache.json, written
        # by the strict run BEFORE generation — 0 extra model calls), and the
        # deterministic align_candidates script extracts the ACTUAL Russian
        # target from the finished chapter translation. Promotion runs
        # strictly after the chapter and BEFORE MemoryManager.promote (GATE),
        # only for chapters with an accepted terminal result (complete /
        # accepted_degraded), exactly like the SAFE-MEMORY entity promote
        # below. The glossary write happens through the existing
        # promote(status, quarantined_chunks), so the B7 quarantined-chunk
        # filter applies to the proposed observations.
        glossary_before = _load_json(memory_dir / "glossary.json", {})
        candidates_block = {
            "generated": 0,
            "proposed": 0,
            "committed": 0,
            "conflicts": 0,
        }
        proposed_sources: set = set()
        # Glossary resolver mode handling (identity-bearing)
        _mode = glossary_resolver_mode
        for _i, _v in enumerate(extra_args):
            if _v == "--glossary-resolver-mode" and _i + 1 < len(extra_args):
                _mode = str(extra_args[_i+1])
            elif isinstance(_v, str) and _v.startswith("--glossary-resolver-mode="):
                _mode = _v.split("=",1)[1]
        _policy = glossary_resolver_cache_miss_policy
        for _i, _v in enumerate(extra_args):
            if _v == "--glossary-resolver-cache-miss-policy" and _i + 1 < len(extra_args):
                _policy = str(extra_args[_i+1])
            elif isinstance(_v, str) and _v.startswith("--glossary-resolver-cache-miss-policy="):
                _policy = _v.split("=",1)[1]
        glossary_sidecar_handled = False
        if _mode == "off":
            LOG.info("glossary_resolver: mode off for %s, new glossary observations forbidden (fail-closed, no legacy deterministic promotion)", chapter_id)
            # Spec glossary-model-resolver: off forbids ANY new glossary observations, even legacy deterministic.
            # Fail-closed: block sidecar handling AND deterministic fallback.
            glossary_sidecar_handled = True
            candidates_block = {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0}
        elif _mode in ("shadow", "promote") and terminal_status in _PROMOTING_STATUSES:
            # Handle sidecar for shadow/promote
            from pact_v4.pipeline.glossary_resolver import (
                load_and_validate_sidecar as _lavs,
                sidecar_path as glossary_sidecar_path,
                semantic_translation_hash as _sth,
                candidate_input_hash as _cih,
                translation_hash as _th,
            )
            import json as _json
            import re as _re
            try:
                _translations = _load_json(out_dir / "translations.json", {})
                _translations_repaired = _load_json(out_dir / "translations_repaired.json", {})
                if not _translations_repaired or not isinstance(_translations_repaired, dict):
                    _translations_repaired = _translations
                _tag_re = _re.compile(r"<[^>]+>")
                def _strip_tags(m):
                    res = {}
                    for k, v in m.items():
                        nv = _tag_re.sub("", v or "")
                        nv = " ".join(nv.split())
                        res[k] = nv
                    return res
                _snapshot_hash = str((run_record.get("identities") or {}).get("snapshot_hash") or "")
                _config_identity = str((run_record.get("identities") or {}).get("config_identity") or "")
                _allowed = None
                _filtered_recs = []
                try:
                    from pact_v4.audit.entity_extractor import ChapterEntityContext, EntityContextCache, entity_context_cache_key, is_entity_glossary_candidate
                    _ec_path = out_dir / "entity_context_cache.json"
                    if _ec_path.exists():
                        _ec_payload = _json.loads(_ec_path.read_text(encoding="utf-8"))
                        _ec_cache = EntityContextCache.from_payload(_ec_payload)
                        _src_map = _source_by_pid(chapter_html)
                        _key = entity_context_cache_key(source_hash=str((run_record.get("identities") or {}).get("source_hash") or ""), extractor_version=str(((run_record.get("operational_policy") or {}).get("audit") or {}).get("extractor_version") or ""))
                        _ctx = _ec_cache.get(_key)
                        if _ctx and _ctx.chapter_id == chapter_id:
                            _filtered_recs = [r for r in _ctx.entities if is_entity_glossary_candidate(r, _src_map)]
                            from pact_v4.pipeline.glossary_resolver import compute_allowed_evidence_pids
                            _allowed = compute_allowed_evidence_pids(_src_map, _filtered_recs)
                except Exception:
                    _allowed = None
                _expected_cand_hash = None
                try:
                    if _allowed is not None and _filtered_recs:
                        _expected_cand_hash = _cih(_filtered_recs)
                except Exception:
                    _expected_cand_hash = None
                _trans_for_val = _translations_repaired if _translations_repaired else _translations
                _expected_model_ref = None
                _expected_backend_identity = None
                try:
                    _be = run_record.get("backend") or {}
                    _bindings = _be.get("model_bindings") or {}
                    for _role in ("russian_selector", "fidelity_reviewer", "qwen_audit", "qwen_fidelity", "default"):
                        if _bindings.get(_role):
                            _expected_model_ref = str(_bindings[_role])
                            break
                    _expected_backend_identity = _be.get("config_identity_hash") or _be.get("identity_hash") or _be.get("backend_identity_hash")
                    if _expected_backend_identity:
                        _expected_backend_identity = str(_expected_backend_identity)
                except Exception:
                    pass
                # Compute expected translation hash BEFORE validation (semantic hash of repaired translation, formatting-aware).
                _expected_translation_hash = None
                try:
                    if _translations_repaired and isinstance(_translations_repaired, dict) and _translations_repaired:
                        _expected_translation_hash = _sth(_strip_tags(_translations_repaired))
                    elif _translations and isinstance(_translations, dict) and _translations:
                        _expected_translation_hash = _sth(_strip_tags(_translations))
                except Exception:
                    _expected_translation_hash = None
                _payload, _err = _lavs(
                    out_dir,
                    expected_chapter_id=chapter_id,
                    expected_snapshot_hash=_snapshot_hash if _snapshot_hash else None,
                    expected_config_identity=_config_identity if _config_identity else None,
                    expected_candidate_input_hash=_expected_cand_hash,
                    expected_translation_hash=_expected_translation_hash,
                    expected_model_ref=_expected_model_ref,
                    expected_backend_identity=_expected_backend_identity,
                    allowed_pids=_allowed,
                    translation_map=_trans_for_val,
                    quarantined_pids=quarantined_pids,
                )
                # stale translation_hash now fails-closed via load_and_validate; no unreachable fallback branch
                if _err is not None:
                    if _err == "missing":
                        LOG.info("glossary no sidecar for %s, fail-closed", chapter_id)
                    else:
                        LOG.warning("glossary sidecar validation failed for %s: %s", chapter_id, _err)
                    candidates_block["generated"] = 0
                    candidates_block["proposed"] = 0
                    candidates_block["conflicts"] = 0
                    glossary_sidecar_handled = True
                else:
                    _proposals = _payload.get("proposals", [])
                    _valid = []
                    for _prop in _proposals:
                        _ev = _prop.get("evidence_pid")
                        if quarantined_pids and _ev in quarantined_pids:
                            LOG.warning("glossary proposal %r quarantined %r for %s, skip", _prop.get("entity"), _ev, chapter_id)
                            continue
                        _ent = _prop.get("entity")
                        _ru = _prop.get("proposed_ru")
                        _existing = glossary_before.get(_ent)
                        _flat = _existing if isinstance(_existing, str) else (_existing.get("target") if isinstance(_existing, dict) else None)
                        if _flat is not None and _flat != _ru:
                            LOG.info("glossary conflict %r existing %r vs %r, skip", _ent, _flat, _ru)
                            continue
                        if _flat == _ru:
                            continue
                        _valid.append(_prop)
                    if _mode == "shadow":
                        LOG.info("glossary shadow for %s: %d proposals logged", chapter_id, len(_valid))
                        candidates_block["generated"] = len(_proposals)
                        candidates_block["proposed"] = len(_valid)
                        candidates_block["conflicts"] = len(_proposals) - len(_valid)
                    elif _mode == "promote":
                        LOG.info("glossary promote for %s: %d proposals", chapter_id, len(_valid))
                        for _prop in _valid:
                            _ent = _prop.get("entity")
                            _ru = _prop.get("proposed_ru")
                            _ptype = _prop.get("type") or "proper_name"
                            _chunk_id = ""
                            try:
                                _pm = _pid_to_chunk(out_dir)
                                if _pm and _prop.get("evidence_pid") in _pm:
                                    _chunk_id = _pm[_prop.get("evidence_pid")]
                            except Exception:
                                _chunk_id = ""
                            manager.add_observation("glossary", _ent, {"target": _ru, "type": _ptype, "chunk_id": _chunk_id})
                            proposed_sources.add(_ent)
                        candidates_block["generated"] = len(_proposals)
                        candidates_block["proposed"] = len(_valid)
                        candidates_block["conflicts"] = len(_proposals) - len(_valid)
                    glossary_sidecar_handled = True
            except Exception as exc:
                LOG.warning("glossary sidecar handling failed for %s: %s", chapter_id, exc)
                glossary_sidecar_handled = True
        else:
            glossary_sidecar_handled = False

        # SAFE-MEMORY (P0 owner decision 2026-08-14, B7 → entity_extractor):
        # the deterministic book_memory_candidates script (B7) is OFF —
        # characters/facts come ONLY from the source-only entity extractor's
        # VERIFIED claims (LLM, prompt "only explicit, evidence quote"),
        # strictly after the chapter and BEFORE MemoryManager.promote (GATE),
        # only for chapters with an accepted terminal result (complete /
        # accepted_degraded). The chapter's strict run already persisted the
        # validated context to entity_context_cache.json (the runner ran the
        # prepass before generation); the run reads that cache — 0 extra
        # model calls. Verified claims → add_observation("book_memory") with
        # extreme-conservative gender (verified only when the extractor's
        # 8-point validation linked pronoun + referent in the same PID);
        # candidate claims are never promoted (audit-only, TIER_B).
        book_memory_before = _load_json(memory_dir / "book_memory.json", {})
        bm_candidates_block = {
            "generated": 0,
            "proposed": 0,
            "committed": 0,
            "conflicts": 0,
        }
        bm_promotions: List[Dict[str, Any]] = []
        entity_obs: Dict[str, Any] = {}
        # GLOSSARY-FROM-ENTITY (owner decision 2026-08-15, variant B): the
        # same validated entity context ALSO feeds the glossary — verified
        # proper-noun entities become candidates, align_candidates (0 model
        # calls) extracts the ACTUAL Russian target from the finished
        # translation, the observation is added to shadow memory and
        # promoted by the same promote() below (B7 quarantined filter
        # applies via chunk_id). The aligned target also fills canonical_ru
        # in the book_memory entity observations.
        # GLOSSARY-FROM-ENTITY fail-closed quarantine provenance (RV
        # finding t_72f549c8, B9-F5/F6): an accepted_degraded chapter WITH
        # quarantined chunks may only generate/promote entity-derived
        # glossary observations when the PID->chunk plan authoritatively
        # maps each source/translation pid exactly once. A
        # missing/corrupt/empty/ambiguous plan (``_pid_to_chunk`` -> None)
        # or an incomplete plan (a source/translation pid the plan does
        # not map) leaves pids of UNKNOWN provenance — they could belong
        # to a quarantined chunk, and an observation carrying
        # ``chunk_id=""`` would slip through the B7 filter and promote
        # quarantine evidence. Such a chapter fails closed for the whole
        # glossary block: no candidates generated/proposed/committed/
        # conflicts, no glossary ledger line, no glossary observation, no
        # glossary mutation (a warning is logged; the run never crashes).
        # Complete chapters and accepted_degraded chapters WITHOUT
        # quarantined chunks never consult the plan (there is no
        # quarantine evidence to exclude) — behaviour unchanged, zero
        # extra model calls.
        glossary_provenance_ok = True
        pid_to_chunk: Optional[Dict[str, str]] = None
        if terminal_status == "accepted_degraded" and quarantined:
            try:
                pid_to_chunk = _pid_to_chunk(out_dir)
                if pid_to_chunk is None:
                    LOG.warning(
                        "GLOSSARY-FROM-ENTITY F6: %s accepted_degraded "
                        "with quarantined chunks %s but PID->chunk "
                        "provenance missing/corrupt/empty/ambiguous "
                        "(duplicate or malformed ownership); failing "
                        "closed — no glossary candidates, ledger line, "
                        "observation or mutation",
                        out_dir.name, sorted(quarantined),
                    )
                    glossary_provenance_ok = False
                else:
                    present_pids = (
                        {str(pid) for pid in _source_by_pid(chapter_html)}
                        | {
                            str(pid)
                            for pid in _load_json(
                                out_dir / "translations.json", {},
                            )
                        }
                    )
                    if not present_pids <= set(pid_to_chunk):
                        LOG.warning(
                            "GLOSSARY-FROM-ENTITY F5: %s accepted_degraded "
                            "with quarantined chunks %s but PID->chunk "
                            "provenance incomplete (plan pids=%d, unmapped "
                            "source/translation pids=%s); failing closed — "
                            "no glossary candidates, ledger line, "
                            "observation or mutation",
                            out_dir.name, sorted(quarantined),
                            len(pid_to_chunk),
                            sorted(present_pids - set(pid_to_chunk)),
                        )
                        glossary_provenance_ok = False
            except Exception as exc:  # noqa: BLE001 — never break a run
                LOG.warning(
                    "GLOSSARY-FROM-ENTITY: provenance gate failed for %s "
                    "(%s: %s); failing closed — no glossary candidates, "
                    "ledger line, observation or mutation",
                    chapter_id, type(exc).__name__, exc,
                )
                glossary_provenance_ok = False
        else:
            pid_to_chunk = _pid_to_chunk(out_dir)
        glossary_obs: Dict[str, Any] = {}
        canonical_ru_map: Dict[str, str] = {}
        glossary_proposed: List[Dict[str, Any]] = []
        glossary_conflicts: List[Dict[str, Any]] = []
        if not glossary_sidecar_handled and terminal_status in _PROMOTING_STATUSES:
            entity_payload = _load_json(out_dir / "entity_context_cache.json", None)
            if isinstance(entity_payload, dict) and entity_payload.get("entries"):
                from pact_v4.audit.entity_extractor import (
                    ChapterEntityContext,
                    EntityContextCache,
                )
                from pact_v4.pipeline.b3_audit_repair import (
                    book_memory_observations_from_entity_context,
                    glossary_observations_from_entity_context,
                )

                try:
                    cache = EntityContextCache.from_payload(entity_payload)
                    # The chapter's context entry (keyed source_hash +
                    # extractor_version); the run may carry several chapters'
                    # entries when resuming — pick the one for THIS chapter
                    # (RV finding 2, fail-closed provenance): the entry must
                    # belong to the CURRENT chapter_id AND its source_hash
                    # must equal the source hash the strict run recorded
                    # (identities.source_hash — the exact SourceArtifact hash
                    # the extractor stamped the context with). A valid cache
                    # entry of ANOTHER chapter — or a stale cache left by an
                    # out_dir reuse — is NEVER promoted under the current
                    # chapter_id: a foreign chapter would corrupt causal
                    # memory/chapter accumulation. No record / no identity /
                    # any mismatch => no promotion (fail-closed).
                    run_source_hash = str(
                        (run_record.get("identities") or {}).get("source_hash") or ""
                    )
                    # RV2 finding 1 (HIGH): the cache identity is
                    # source_hash + extractor_version (pact_v4/audit/
                    # entity_extractor.py:entity_context_cache_key) — a
                    # STALE entry of the same chapter/source_hash written by
                    # an older extractor_version must never be promoted
                    # alongside the current one. The expected version is the
                    # one the strict run actually ran with:
                    # run_record.operational_policy.audit.extractor_version
                    # (the runner records it at record build). No record /
                    # no identity / any mismatch => no promotion
                    # (fail-closed), mirroring the source_hash rule above.
                    run_extractor_version = str(
                        ((run_record.get("operational_policy") or {})
                         .get("audit") or {}).get("extractor_version") or ""
                    )
                    for entry in cache.to_payload().get("entries", []):
                        if not isinstance(entry, dict) or "context" not in entry:
                            continue
                        context = ChapterEntityContext.from_payload(entry["context"])
                        if context.chapter_id != chapter_id:
                            continue
                        if not run_source_hash or context.source_hash != run_source_hash:
                            continue
                        if (
                            not run_extractor_version
                            or context.extractor_version != run_extractor_version
                        ):
                            continue
                        obs = book_memory_observations_from_entity_context(
                            context, chapter_id=chapter_id,
                        )
                        entity_obs.update(obs.get("book_memory", {}))
                        try:
                            # GLOSSARY-FROM-ENTITY (variant B, 0 extra model
                            # calls): align the verified proper-noun entities
                            # against the finished chapter translation.
                            # Fail-closed provenance (RV finding
                            # t_72f549c8, B9-F5/F6): when the chapter is
                            # accepted_degraded WITH quarantined chunks, the
                            # PID->chunk plan must be authoritative and
                            # complete (checked above) — otherwise no
                            # glossary observations are generated at all
                            # (chunk_id="" would bypass the B7 filter and
                            # could promote quarantine evidence).
                            if glossary_provenance_ok:
                                g = glossary_observations_from_entity_context(
                                    context,
                                    chapter_id=chapter_id,
                                    source_by_pid=_source_by_pid(chapter_html),
                                    translations=_load_json(
                                        out_dir / "translations.json", {}
                                    ),
                                    glossary=glossary_before,
                                    book_memory=book_memory_before,
                                    consensus_ratio=consensus_ratio,
                                    pid_to_chunk=pid_to_chunk,
                                    _allow_proper_name_align=True,
                                )
                                glossary_obs.update(g["glossary"])
                                canonical_ru_map.update(g["canonical_ru"])
                                glossary_proposed.extend(g["proposed"])
                                glossary_conflicts.extend(g["conflicts"])
                        except Exception as exc:  # noqa: BLE001 — never break a run
                            LOG.warning(
                                "GLOSSARY-FROM-ENTITY: glossary alignment "
                                "skipped for %s (%s: %s); no glossary "
                                "promotion for this chapter",
                                chapter_id, type(exc).__name__, exc,
                            )
                except Exception as exc:  # noqa: BLE001 — never break a run
                    LOG.warning(
                        "SAFE-MEMORY: entity_context_cache.json for %s "
                        "unreadable/foreign (%s: %s); no book_memory promote",
                        chapter_id, type(exc).__name__, exc,
                    )
            # canonical_ru: the same alignment fills canonical_ru in the
            # book_memory entity observations (owner decision 2026-08-15).
            for ename, target in canonical_ru_map.items():
                for section in ("entities", "characters"):
                    key = f"{section}:{ename}"
                    if key in entity_obs and isinstance(entity_obs[key], dict):
                        entity_obs[key] = dict(entity_obs[key])
                        entity_obs[key]["canonical_ru"] = target
            # Glossary observations -> shadow memory (promoted below via the
            # same promote(status, quarantined) call, B7 filter applies).
            for source, value in glossary_obs.items():
                manager.add_observation("glossary", source, value)
                proposed_sources.add(source)
            if glossary_proposed or glossary_conflicts:
                ledger.append_chapter(
                    chapter_id, glossary_proposed + glossary_conflicts,
                )
            candidates_block["generated"] = (
                len(glossary_proposed) + len(glossary_conflicts)
            )
            candidates_block["proposed"] = len(glossary_proposed)
            candidates_block["conflicts"] = len(glossary_conflicts)
            for key, value in entity_obs.items():
                # Chapter accumulation: a character/entity seen in earlier
                # chapters keeps its cumulative `chapters` list (the entity
                # cache holds only THIS chapter's context; union with the
                # already-promoted entry so `chapters` never shrinks).
                section, _, entry_key = key.partition(":")
                if section in ("characters", "entities") and isinstance(value, dict):
                    existing = book_memory_before.get(section, {}).get(entry_key)
                    if isinstance(existing, dict):
                        merged = dict(value)
                        merged["chapters"] = sorted({
                            str(c) for c in (
                                list(existing.get("chapters") or [])
                                + list(value.get("chapters") or [])
                            )
                        })
                        # Cross-chapter gender disagreement fails closed
                        # (owner decision 2026-08-08, BM): a verified gender
                        # contradicted by a LATER chapter is unknown forever
                        # — never pick a winner.
                        existing_gender = existing.get("gender")
                        new_gender = value.get("gender")
                        if (
                            existing_gender
                            and new_gender
                            and existing_gender != new_gender
                        ):
                            merged.pop("gender", None)
                        elif existing_gender and not new_gender:
                            merged["gender"] = existing_gender
                        value = merged
                manager.add_observation("book_memory", key, value)
            bm_candidates_block["generated"] = len(entity_obs)
            bm_candidates_block["proposed"] = len(entity_obs)
            bm_promotions = [
                {
                    "source": str(key.partition(":")[2]),
                    "kind": str(value.get("type") or "entity"),
                    "gender": value.get("gender"),
                    "chapters": list(value.get("chapters") or []),
                    "evidence_pids": list(value.get("source_pids") or []),
                    "context": str(value.get("fact") or ""),
                }
                for key, value in entity_obs.items()
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

        # A2 (causal <N, P0 2026-08-14; RV finding 1 SAFE-MEMORY
        # 2026-08-14): after an ACCEPTED chapter N, write chapter_index
        # entries for BOTH the current chapter N and the NEXT chapter N+1
        # (when it exists in this run), each built from PRE-chapter memory
        # ONLY — facts of chapters < the entry's chapter id
        # (pre_chapter_book_memory). The strict runner reads
        # chapter_index[X] before generating X, so:
        #   * entry[N] (re)built from pre-N memory: a rerun of N never
        #     sees N's own post-promotion facts (the old post-promotion
        #     build leaked them into N's own prompt);
        #   * entry[N+1] pre-built NOW from the post-N memory (= the
        #     pre-chapter memory of N+1): the FIRST run of N+1 already
        #     sees the memory of accepted chapters < N+1 (the old code
        #     built only the current chapter's entry after acceptance, so
        #     N+1's first run failed soft to narrator+seed and saw none of
        #     the prior chapters' memory).
        # 0 model calls, deterministic (B9-A2 card). Failed chapters never
        # touch the index.
        if terminal_status in _PROMOTING_STATUSES:
            try:
                from pact_full_pipeline_runner_v1.build_chapter_index import (
                    build_index_file,
                )

                build_index_file(
                    memory_dir=str(memory_dir),
                    chapter_html=str(chapter_html),
                    chapter_id=chapter_id,
                    out_path=str(memory_dir / "chapter_index.json"),
                )
                index_built = True
            except Exception as exc:  # noqa: BLE001 — never break a run
                LOG.warning(
                    "A2: chapter_index build failed for %s (%s: %s); "
                    "causal bible will fail-soft to narrator+seed",
                    chapter_id, type(exc).__name__, exc,
                )
                index_built = False
            # RV finding 1: pre-build the NEXT chapter's entry so its
            # FIRST run sees the memory of accepted chapters < N+1
            # (best-effort — a missing next-html or build error must
            # never fail the run; the next chapter then fails soft).
            next_chapter_id = _next_chapter_id(chapter_id, chapter_ids)
            if next_chapter_id:
                try:
                    from pact_full_pipeline_runner_v1.build_chapter_index import (
                        build_index_file,
                    )

                    build_index_file(
                        memory_dir=str(memory_dir),
                        chapter_html=str(
                            chapter_html_pattern.format(chapter_id=next_chapter_id)
                        ),
                        chapter_id=next_chapter_id,
                        out_path=str(memory_dir / "chapter_index.json"),
                    )
                except Exception as exc:  # noqa: BLE001 — never break a run
                    LOG.warning(
                        "A2: next-chapter index prebuild failed for %s "
                        "(%s: %s); %s will fail-soft to narrator+seed",
                        next_chapter_id, type(exc).__name__, exc,
                        next_chapter_id,
                    )
        else:
            index_built = False

        # Media sync post-promote hook: push updated state after promote
        media_confirmation: Optional[Dict[str, Any]] = None
        media_error: Optional[str] = None
        if media_book_id is not None and promoted:
            from pact_v4.snapshot.run_hooks import post_promote_push
            try:
                _exec_host2 = media_exec_host if media_exec_host is not None else _detect_execution_host()
                media_confirmation = post_promote_push(
                    media_book_id,
                    memory_dir,
                    transport=media_transport,
                    ssh_target=media_target,
                    root=media_root,
                    max_retries=media_max_retries,
                    execution_host=_exec_host2,
                )
            except Exception as e:
                # Preserve local state, report error, do not crash run
                media_error = str(e)
                LOG.error("Media sync push failed for chapter %s: %s", chapter_id, e)

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

        # BM: committed = how many of the proposed entity observations
        # actually landed in book_memory.json after promote (SAFE-MEMORY,
        # P0 2026-08-14 — the deterministic B7 script is OFF; the proposed
        # set is the entity extractor's verified observations). A
        # character/entity whose entry exists in its section (with the
        # proposed value, chapter accumulation included) and a fact whose
        # text is present in the facts list count as committed. An entry
        # blocked by established/locked conflict resolution does not count.
        book_memory_after = _load_json(memory_dir / "book_memory.json", {})
        committed = 0
        for key, value in entity_obs.items():
            section, _, entry_key = key.partition(":")
            if section in ("characters", "entities") and isinstance(value, dict):
                stored = (book_memory_after.get(section) or {}).get(entry_key)
                if not isinstance(stored, dict):
                    continue
                if stored.get("gender") != value.get("gender"):
                    continue
                stored_chapters = stored.get("chapters") or []
                if set(stored_chapters) >= set(value.get("chapters") or []):
                    committed += 1
            elif section == "facts" and isinstance(value, dict):
                fact_text = value.get("fact")
                if fact_text and any(
                    isinstance(f, dict) and f.get("fact") == fact_text
                    for f in book_memory_after.get("facts", [])
                ):
                    committed += 1
        bm_candidates_block["committed"] = committed

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
            index_built=index_built,
            error=error_msg,
            media_confirmation=media_confirmation,
            media_error=media_error,
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
    parser.add_argument(
        "--promote-existing", type=Path, default=None, metavar="CHAPTER_DIR",
        help="B1 promote-only: REUSE an already-completed strict chapter "
             "out_dir (strict_chapter_trial_record.json + translations.json "
             "+ entity_context_cache.json) instead of running the strict "
             "pipeline, then run the standard acceptance/promotion stage "
             "(entity/glossary/book_memory/chapter_index). Use this when the "
             "translator model (e.g. Muse) is no longer available. Only one "
             "chapter is supported in this mode; --chapters width is ignored.",
    )
    parser.add_argument("--media-book-id", type=str, default=None, help="Media sync: book-id for RT<->media state sync (requires media SSH)")
    parser.add_argument("--media-root", type=str, default="/home/rt/pact_runs", help="Media store root")
    parser.add_argument("--media-target", type=str, default="media", help="SSH target for media (default 'media')")
    parser.add_argument("--media-exec-host", type=str, default=None, choices=["media", "rt"], help="Trusted execution host for media sync (media vs rt); auto-detected from platform/env when omitted")
    # v41 formatting (lenient debt) — mirror V3 formatting cfg, max incidents soft by default
    parser.add_argument("--max-formatting-incidents", type=int, default=_DEFAULT_MAX_FORMATTING_INCIDENTS, help="v41 italics: max formatting incidents before blocking (lenient debt default 999)")
    parser.add_argument("--formatting-enabled", action="store_true", default=True, help="Enable v41 italics formatting (default: enabled)")
    parser.add_argument("--no-formatting", dest="formatting_enabled", action="store_false", help="Disable v41 italics formatting")
    parser.add_argument("--glossary-resolver-mode", choices=("off", "shadow", "promote"), default="off", help="Glossary resolver mode (identity-bearing, default off): off/shadow/promote")
    parser.add_argument("--glossary-resolver-cache-miss-policy", choices=("recompute", "fail_closed"), default="recompute", help="Glossary resolver cache-miss policy (identity-bearing, default recompute)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argparser()
    args, extra = parser.parse_known_args(argv)
    # B5 manual allowlist: consumed here for B9 candidate exclusion, and
    # re-forwarded to the strict driver so the real book-run allowlist is
    # exactly the same input (no divergent duplicate flag).
    for entry in (args.mixed_script_allow or ()):
        extra += ["--mixed-script-allow", str(entry)]
    _fmt_cfg: dict = dict(_DEFAULT_FORMATTING_CFG)
    _fmt_cfg["enabled"] = bool(args.formatting_enabled)
    # Per-chapter formatting lifecycle (book-formatting-remote-server): no global
    # formatting_client — built inside run_book at formatting stage per out_dir/server_logs.
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
        promote_existing_dir=args.promote_existing,
        media_book_id=args.media_book_id,
        media_root=args.media_root,
        media_target=args.media_target,
        media_exec_host=args.media_exec_host,
        formatting_cfg=_fmt_cfg,
        formatting_client=None,
        max_formatting_incidents=int(args.max_formatting_incidents),
        glossary_resolver_mode=args.glossary_resolver_mode,
        glossary_resolver_cache_miss_policy=args.glossary_resolver_cache_miss_policy,
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
    # Fail-closed media sync + final cross-host publication verdict
    media_failed = 0
    if args.media_book_id is not None:
        promoted = [r for r in result["chapters"] if r.get("promoted")]
        if promoted:
            accepted = []
            rejected = []
            for rec in promoted:
                if rec.get("media_error"):
                    rejected.append((rec["chapter_id"], str(rec["media_error"])[:500]))
                elif not rec.get("media_confirmation"):
                    rejected.append((rec["chapter_id"], "missing confirmation"))
                else:
                    conf = rec["media_confirmation"]
                    if conf.get("status") != "ACCEPTED":
                        reason = conf.get("reason") or conf.get("message") or json.dumps(conf)[:500]
                        rejected.append((rec["chapter_id"], str(reason)[:500]))
                    else:
                        rev = conf.get("revision_id") or conf.get("revision") or "unknown"
                        accepted.append((rec["chapter_id"], str(rev)))
            # Machine-readable: patch book_run.json with global media_publish verdict
            try:
                _br_path = Path(args.out_base) / "book_run.json"
                if _br_path.is_file():
                    _br_data = json.loads(_br_path.read_text(encoding="utf-8"))
                    if rejected:
                        _br_data["media_publish"] = {
                            "status": "REJECTED",
                            "accepted": [{"chapter_id": cid, "revision_id": rev} for cid, rev in accepted],
                            "rejected": [{"chapter_id": cid, "reason": msg} for cid, msg in rejected],
                        }
                    else:
                        _br_data["media_publish"] = {
                            "status": "ACCEPTED",
                            "accepted": [{"chapter_id": cid, "revision_id": rev} for cid, rev in accepted],
                            "rejected": [],
                        }
                    _br_path.write_text(json.dumps(_br_data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            if rejected:
                media_failed = len(rejected)
                details = "; ".join(f"{cid}: {msg}" for cid, msg in rejected)
                if accepted:
                    details += "; accepted: " + ", ".join(f"{cid}={rev}" for cid, rev in accepted)
                # Human-readable final verdict (required)
                print(f"MEDIA PUBLISH: REJECTED {details}")
                # Machine-readable final verdict (required)
                print(json.dumps({"media_publish": {"status": "REJECTED", "rejected": [{"chapter_id": cid, "reason": msg} for cid, msg in rejected], "accepted": [{"chapter_id": cid, "revision_id": rev} for cid, rev in accepted]}}, ensure_ascii=False))
            else:
                details = ", ".join(f"{cid}={rev}" for cid, rev in accepted)
                print(f"MEDIA PUBLISH: ACCEPTED revision={details}")
                print(json.dumps({"media_publish": {"status": "ACCEPTED", "accepted": [{"chapter_id": cid, "revision_id": rev} for cid, rev in accepted], "rejected": []}}, ensure_ascii=False))
    if failed:
        print(
            f"\n{failed} chapter(s) did not reach complete/accepted_degraded. "
            "See book_run.json for details.",
            file=sys.stderr,
        )
        return 1
    if media_failed:
        print(
            f"\n{media_failed} chapter(s) failed media sync confirmation "
            f"(media_error/missing confirmation). See book_run.json for details.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
