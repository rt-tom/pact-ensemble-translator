"""V4.1 BM: deterministic book_memory candidate generation + cross-chapter ledger.

Card BM (§15 of ``docs/plans/V4_1_AUDIT_B1_RU.md``; owner decision 2026-08-08:
0 model calls). After each chapter with an accepted terminal result
(``complete`` / ``accepted_degraded``) the book run derives candidate
book_memory entries from the chapter SOURCE text (never from translations or
LLM inference — risk of poisoning book_memory, lesson «The Nurse: female»)
and accumulates them cross-chapter in an append-only ledger
(``book_memory_candidates.json``). Candidates that meet the v3-style
thresholds (character: total occurrences >= N OR distinct chapters >= M) are
proposed via ``MemoryManager.add_observation("book_memory", ...)`` and
promoted by the existing ``promote`` path (B7): established/locked entries
are never overwritten, the quarantined-chunk filter applies, and
``book_memory.json`` is only rewritten when a value really changes
(byte preservation, B9-RV9).

Candidate categories (all deterministic, all fail-closed):

* **characters** — proper names from the chapter source (capitalized
  mid-sentence, title-case, never lowercase, not a stop word) that are NOT
  already book_memory characters/entities (or variants) and NOT glossary
  keys / B5 mixed-script allowlist tokens. A character is a promotion
  candidate once its cumulative ledger record reaches ``min_name_occurrences``
  total occurrences OR ``min_name_chapters`` distinct chapters.
* **gender** — attached to a character candidate ONLY when the source
  explicitly uses gendered pronouns (he/him/his = male; she/her/hers =
  female) in the same or immediately adjacent PID of a name occurrence
  (spec: «he/she/him/her в соседних PID»). Fail-closed: no pronoun evidence,
  or BOTH genders present near the name, or chapters disagreeing on the
  gender (cumulative) => no gender is ever promoted.
* **facts** — deterministic key-bound fact entries promoted together with a
  character: a presence fact (``X appears in chapters ...``) and, when the
  gender is source-confirmed, a gender fact. Both carry explicit ``keys`` so
  ``build_chapter_index`` can bind them to the character in later chapters.
* **narrator / narrator gender** — NEVER generated here (fail-closed, like
  locked): the narrator is already in book_memory (``pov``) and its gender
  must come from the owner-maintained bible, not from inference.

Safety invariants (spec §15): a candidate without explicit source
confirmation is never promoted; ``book_memory_hash`` changes only when a
real promotion writes the file; zero model calls, no HTTP, no side effects
on import.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.glossary_candidates import (
    EN_STOP_WORDS,
    _WORD_TOKEN_RE,
    _extract_context,
    _glossary_terms,
    _is_sentence_start,
    _memory_terms,
    candidate_key,
    _norm,
    _strip_html,
)

# Thresholds by analogy with B9 (proper_name >= 2 occurrences; term >= 2
# chapters). A character promotes at N total occurrences OR M distinct
# chapters; both calibrated after the first real book-run.
DEFAULT_MIN_NAME_OCCURRENCES = 2
DEFAULT_MIN_NAME_CHAPTERS = 2

LEDGER_VERSION = 1

# Explicit gendered pronouns counted as gender evidence (spec: «he/she/him/her»).
_MALE_PRONOUNS = frozenset({"he", "him", "his"})
_FEMALE_PRONOUNS = frozenset({"she", "her", "hers"})
_PRONOUN_RE = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)?")


def _pronoun_votes(text: str) -> Dict[str, int]:
    """``{male: n, female: n}`` gendered-pronoun counts in ``text``."""
    votes = {"male": 0, "female": 0}
    for token in _PRONOUN_RE.findall(text or ""):
        folded = token.casefold()
        if folded in _MALE_PRONOUNS:
            votes["male"] += 1
        elif folded in _FEMALE_PRONOUNS:
            votes["female"] += 1
    return votes


def _gender_from_window(texts: Sequence[str]) -> Optional[str]:
    """Single gender from explicit pronoun evidence, else ``None`` (fail-closed).

    Returns ``"male"`` when ONLY male pronouns occur, ``"female"`` when ONLY
    female pronouns occur. Both genders present (or none) => ambiguous /
    unconfirmed => ``None`` (spec: «пол — только если source явно»).
    """
    male = 0
    female = 0
    for text in texts:
        votes = _pronoun_votes(text)
        male += votes["male"]
        female += votes["female"]
    if male > 0 and female == 0:
        return "male"
    if female > 0 and male == 0:
        return "female"
    return None


def _surface_forms(blocks: Sequence[Tuple[str, str]], name: str) -> List[str]:
    """Distinct surface forms of ``name`` in the pid blocks (case-insensitive)."""
    folded = name.casefold()
    forms: List[str] = []
    seen: set = set()
    for _pid, text in blocks:
        for token in _WORD_TOKEN_RE.findall(text):
            if token.casefold() == folded and token not in seen:
                seen.add(token)
                forms.append(token)
    return forms


def generate_book_memory_candidates(
    source: Any,
    *,
    book_memory: Any = None,
    glossary: Any = None,
    allowlist: Iterable[str] = (),
    pid_to_chunk: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Deterministic scan of a chapter's EN source for book_memory candidates.

    ``source`` is the chapter source: a plain string (raw HTML or text), a
    ``{pid: text}`` mapping, a sequence of ``(pid, text)`` pairs, or a
    sequence of ``SourceBlock`` objects. ``pid_to_chunk`` maps pids to
    ``chunk_id`` so candidates carry the chunks their evidence lives in.

    Every proper-name occurrence is recorded (no per-chapter frequency
    filter): the v3-style promotion thresholds (total occurrences >= N OR
    distinct chapters >= M) are applied by the book run on the CUMULATIVE
    ledger record, so a name that appears once per chapter across M chapters
    can still promote.

    Exclusions (like B9): established book_memory characters/entities and
    their variants, glossary keys, and the B5 mixed-script allowlist tokens.

    Returns a list of candidate records, deterministically ordered by
    (casefolded source):

      ``{source, kind: "character", occurrences, chunk_ids, evidence_pids,
        gender, gender_evidence_pids, context}``

    ``gender`` is ``"male"`` / ``"female"`` only when the source explicitly
    shows exclusively one gendered pronoun set in the name's PIDs and their
    immediate neighbours, else ``None``. No model calls, no I/O.
    """
    blocks, full_text = _normalize_source(source)
    if not full_text:
        return []

    exclude: set = set()
    exclude.update(t.casefold() for t in _glossary_terms(glossary))
    exclude.update(t.casefold() for t in _memory_terms(book_memory))
    exclude.update(_norm(str(item)).casefold()
                   for item in allowlist if _norm(str(item)))
    # Narrator is fail-closed: never a candidate (already in book_memory pov).
    if isinstance(book_memory, Mapping):
        pov = book_memory.get("pov")
        if isinstance(pov, Mapping):
            narrator = pov.get("source_name")
            if narrator:
                exclude.add(_norm(str(narrator)).casefold())

    # Per-folded-token statistics over the token stream.
    counts: Dict[str, int] = {}
    first_seen: Dict[str, str] = {}
    capitalized_mid: set = set()
    has_title_case: set = set()
    appears_lowercase: set = set()
    tokens_in_blocks: Dict[str, List[str]] = {}

    def _scan(text: str, pid: Optional[str]) -> None:
        for match in _WORD_TOKEN_RE.finditer(text):
            token = match.group()
            before = text[max(0, match.start() - 60):match.start()]
            if token.casefold().endswith(("'s", "\u2019s")):
                token = token[:-2]
            elif "'" in token or "\u2019" in token:
                root = token.split("'")[0].split("\u2019")[0].casefold()
                if root in EN_STOP_WORDS:
                    continue
            folded = token.casefold()
            counts[folded] = counts.get(folded, 0) + 1
            if folded not in first_seen:
                first_seen[folded] = token
            if token[0].isupper() and not _is_sentence_start(before):
                capitalized_mid.add(folded)
            if token[0].isupper() and not token.isupper():
                has_title_case.add(folded)
            if token[0].islower():
                appears_lowercase.add(folded)
            if pid is not None:
                tokens_in_blocks.setdefault(folded, []).append(pid)

    if blocks is None:
        _scan(full_text, None)
    else:
        for pid, text in blocks:
            _scan(text, pid)

    pid_order = [pid for pid, _ in blocks] if blocks else []
    pid_text = dict(blocks) if blocks else {}

    candidates: List[Dict[str, Any]] = []
    for folded, occurrences in counts.items():
        if folded in exclude or folded in EN_STOP_WORDS:
            continue
        if len(folded) < 2:
            continue
        is_name = (folded in capitalized_mid and folded in has_title_case
                   and folded not in appears_lowercase)
        if not is_name:
            continue
        source_form = first_seen[folded]
        evidence_pids = sorted(set(tokens_in_blocks.get(folded, ())))
        chunk_ids: List[str] = []
        if blocks is not None and pid_to_chunk:
            chunk_ids = sorted({pid_to_chunk[pid] for pid in evidence_pids
                                if pid in pid_to_chunk})
        # Gender: pronouns in the name's PIDs and their immediate neighbours
        # (spec «в соседних PID»). Fail-closed: both genders or none => None.
        window: List[str] = []
        for pid in evidence_pids:
            if pid not in pid_text:
                continue
            window.append(pid_text[pid])
            idx = pid_order.index(pid) if pid in pid_order else -1
            for nxt in (idx - 1, idx + 1):
                if 0 <= nxt < len(pid_order):
                    window.append(pid_text[pid_order[nxt]])
        gender = _gender_from_window(window)
        gender_evidence_pids: List[str] = []
        if gender is not None:
            for pid in evidence_pids:
                if pid not in pid_text:
                    continue
                idx = pid_order.index(pid) if pid in pid_order else -1
                near = [pid_text[pid]]
                for nxt in (idx - 1, idx + 1):
                    if 0 <= nxt < len(pid_order):
                        near.append(pid_text[pid_order[nxt]])
                votes = _pronoun_votes(" ".join(near))
                wanted = votes["male"] if gender == "male" else votes["female"]
                if wanted > 0:
                    gender_evidence_pids.append(pid)
            gender_evidence_pids = sorted(set(gender_evidence_pids))
        candidates.append({
            "source": source_form,
            "kind": "character",
            "occurrences": occurrences,
            "chunk_ids": chunk_ids,
            "evidence_pids": evidence_pids,
            "gender": gender,
            "gender_evidence_pids": gender_evidence_pids,
            "context": _extract_context(full_text, source_form),
        })

    candidates.sort(key=lambda c: (c["source"].casefold(), c["kind"]))
    return candidates


def _normalize_source(source: Any) -> Tuple[Optional[List[Tuple[str, str]]], str]:
    """Normalize the generator input into (blocks, full_text)."""
    if isinstance(source, str):
        return None, _strip_html(source)
    if isinstance(source, Mapping):
        blocks = [(str(pid), _strip_html(str(text)))
                  for pid, text in source.items()]
    else:
        blocks = []
        for item in source:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                blocks.append((str(item[0]), _strip_html(str(item[1]))))
            else:  # duck-typed SourceBlock (pid / text attributes)
                blocks.append((str(item.pid), _strip_html(str(item.text))))
    if not blocks:
        return blocks, ""
    seen: Dict[str, int] = {}
    ordered: List[Tuple[str, str, int]] = []
    for pid, text in blocks:
        if pid not in seen:
            seen[pid] = len(ordered)
            ordered.append((pid, text, seen[pid]))
        else:
            idx = seen[pid]
            ordered[idx] = (pid, ordered[idx][1] + " " + text, idx)
    sorted_blocks = [(pid, text) for pid, text, _ in
                     sorted(ordered, key=lambda item: (item[0], item[2]))]
    return sorted_blocks, " ".join(text for _, text in sorted_blocks)


class BookMemoryCandidateLedger:
    """Append-only, line-based cross-chapter candidate accumulator.

    ``book_memory_candidates.json`` holds one JSON object per line — a
    chapter observation for one candidate. Loading parses complete lines and
    merges them by ``candidate_key(source, kind)`` into v3-style records:

      ``{source, kind, total_occurrences,
        chapters: [{chapter_id, chunk_ids, count, evidence_pids, gender,
                    gender_evidence_pids}],
        gender, genders_seen, gender_conflicts, first_context}``

    ``gender`` is the single distinct non-None per-chapter gender and is
    irreversible (fail-closed, like the B9 glossary target): once two
    chapters resolve the character to DIFFERENT genders it stays ``None``
    forever and the disagreeing genders are emitted in ``gender_conflicts``.
    A chapter whose source shows no or ambiguous pronoun evidence has
    ``gender=None`` and does not poison the merged record.

    Crash-safety mirrors ``GlossaryCandidateLedger``: a torn partial trailing
    line is skipped on load; re-observing the same (chapter, candidate)
    updates that chapter's line in place (idempotent re-runs).
    """

    def __init__(self, path: str):
        self.path = path

    # -- reading -----------------------------------------------------------

    def load(self) -> Dict[str, Dict[str, Any]]:
        """Merged records keyed by ``candidate_key(source, kind)``."""
        observations = self._load_lines()
        return self.merge({}, observations)

    def _read_raw_lines(self) -> List[str]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            return f.readlines()

    def _load_lines(self) -> List[Dict[str, Any]]:
        raw_lines = self._read_raw_lines()
        observations: List[Dict[str, Any]] = []
        for index, line in enumerate(raw_lines):
            line = line.strip()
            if not line:
                continue
            try:
                observations.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if index == len(raw_lines) - 1:
                    continue  # torn trailing line from an interrupted append
                raise ValueError(
                    f"Corrupt book_memory candidate ledger line {index + 1} in "
                    f"{self.path}: {exc}"
                ) from exc
        return observations

    # -- merging (pure) ----------------------------------------------------

    @staticmethod
    def merge(
        left: Mapping[str, Any], right: Any,
    ) -> Dict[str, Dict[str, Any]]:
        """Merge observation sources into merged records (idempotent)."""
        collapsed: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if isinstance(right, Mapping):
            source_items = list(dict(left).items()) + list(dict(right).items())
        else:
            source_items = list(dict(left).items()) + [
                (candidate_key(str(item.get("source") or ""),
                               str(item.get("kind") or "")), item)
                for item in right
                if isinstance(item, dict)
            ]
        for key, value in source_items:
            obs_list = value if isinstance(value, list) else [value]
            for item in obs_list:
                if not isinstance(item, dict):
                    continue
                chapter_id = str(item.get("chapter_id") or "")
                collapsed[(key, chapter_id)] = item
        merged: Dict[str, Dict[str, Any]] = {}
        for (key, _chapter_id), item in collapsed.items():
            merged[key] = BookMemoryCandidateLedger._merge_one(
                merged.get(key), item
            )
        return merged

    @staticmethod
    def _merge_one(
        record: Optional[Dict[str, Any]], obs: Dict[str, Any],
    ) -> Dict[str, Any]:
        source = str(obs.get("source") or "")
        kind = str(obs.get("kind") or "character")
        occurrences = int(obs.get("occurrences") or 0)
        chapter_id = str(obs.get("chapter_id") or "")
        chunk_ids = sorted({str(c) for c in (obs.get("chunk_ids") or [])})
        evidence_pids = sorted({str(p) for p in (obs.get("evidence_pids") or [])})
        gender = obs.get("gender")
        gender_evidence_pids = sorted(
            {str(p) for p in (obs.get("gender_evidence_pids") or [])}
        )
        context = str(obs.get("context") or "")

        if record is None:
            record = {
                "source": source,
                "kind": kind,
                "total_occurrences": 0,
                "chapters": [],
                "gender": None,
                "genders_seen": [],
                "gender_conflicts": [],
                "first_context": "",
            }

        record["source"] = record.get("source") or source
        record["kind"] = record.get("kind") or kind
        record["total_occurrences"] = int(record.get("total_occurrences") or 0) + occurrences

        chapters = list(record.get("chapters") or [])
        replaced = False
        for entry in chapters:
            if isinstance(entry, dict) and entry.get("chapter_id") == chapter_id:
                entry["count"] = occurrences
                entry["chunk_ids"] = chunk_ids
                entry["evidence_pids"] = evidence_pids
                entry["gender"] = gender
                entry["gender_evidence_pids"] = gender_evidence_pids
                replaced = True
                break
        if not replaced and chapter_id:
            chapters.append({
                "chapter_id": chapter_id,
                "chunk_ids": chunk_ids,
                "count": occurrences,
                "evidence_pids": evidence_pids,
                "gender": gender,
                "gender_evidence_pids": gender_evidence_pids,
            })
        record["chapters"] = chapters

        record["first_context"] = record.get("first_context") or context

        genders_seen = record.get("genders_seen")
        if not isinstance(genders_seen, list):
            genders_seen = []
            if record.get("gender") is not None:
                genders_seen.append(str(record["gender"]))
            record["genders_seen"] = genders_seen
        if gender is not None and str(gender) not in genders_seen:
            genders_seen.append(str(gender))
        record["gender"] = BookMemoryCandidateLedger._merge_gender(
            genders_seen
        )
        if len(genders_seen) > 1:
            record["gender_conflicts"] = sorted(set(genders_seen))
        return record

    @staticmethod
    def _merge_gender(genders_seen: List[str]) -> Optional[str]:
        """Merged gender: single distinct non-None, else ``None`` forever."""
        if len(genders_seen) == 1:
            return genders_seen[0]
        return None

    # -- writing -----------------------------------------------------------

    def append_chapter(
        self, chapter_id: str, candidates: Sequence[Mapping[str, Any]],
    ) -> Dict[str, int]:
        """Append one line per candidate for ``chapter_id`` (idempotent)."""
        if not chapter_id:
            raise ValueError("chapter_id is required")
        raw_lines = self._read_raw_lines()
        line_by_key: Dict[str, int] = {}
        torn_trailing: Optional[int] = None
        for index, line in enumerate(raw_lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obs = json.loads(stripped)
            except json.JSONDecodeError:
                if index == len(raw_lines) - 1:
                    torn_trailing = index
                    continue
                raise
            key = candidate_key(str(obs.get("source") or ""),
                                str(obs.get("kind") or ""))
            if str(obs.get("chapter_id")) == chapter_id:
                line_by_key[key] = index

        stats = {"appended": 0, "new_candidates": 0, "updated": 0}
        for candidate in candidates:
            source = str(candidate.get("source") or "")
            kind = str(candidate.get("kind") or "character")
            if not source:
                continue
            key = candidate_key(source, kind)
            obs = {
                "chapter_id": chapter_id,
                "source": source,
                "kind": kind,
                "occurrences": int(candidate.get("occurrences") or 0),
                "chunk_ids": sorted({str(c) for c in (candidate.get("chunk_ids") or [])}),
                "evidence_pids": sorted({str(p) for p in (candidate.get("evidence_pids") or [])}),
                "gender": candidate.get("gender"),
                "gender_evidence_pids": sorted(
                    {str(p) for p in (candidate.get("gender_evidence_pids") or [])}
                ),
                "context": str(candidate.get("context") or ""),
            }
            line = json.dumps(obs, ensure_ascii=False)
            if key in line_by_key:
                raw_lines[line_by_key[key]] = line
                stats["updated"] += 1
            else:
                if torn_trailing is not None:
                    del raw_lines[torn_trailing]
                    torn_trailing = None
                raw_lines.append(line)
                stats["appended"] += 1
                stats["new_candidates"] += 1

        if torn_trailing is not None:
            del raw_lines[torn_trailing]

        self._write_lines(raw_lines)
        return stats

    def _write_lines(self, lines: List[str]) -> None:
        dir_name = os.path.dirname(self.path) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
