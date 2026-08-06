"""B9-I1: deterministic glossary-candidate generation + consensus alignment + ledger.

V4 B9 (owner-approved 2026-08-04, ``docs/plans/V4_B9_GLOSSARY_OBSERVATIONS_TASK_RU.md``)
splits the v3 glossary-candidate mechanics into a standalone module (this one,
I1) and a later production integration (I2: ``MemoryManager.add_observation``
calls in the book run). This module is the model-free core:

  1. ``generate_candidates`` — frequency/regex scan of a chapter's EN source
     text: ``proper_name`` tokens (capitalized mid-sentence, >= 2 occurrences)
     and ``term`` tokens (>= 3 occurrences, length >= 4, not stop-words), with
     exclusions for established glossary keys, book-memory characters and
     their variants, and the B5 mixed-script allowlist.
  2. ``align_candidates`` — consensus alignment of the Russian ``target`` from
     a finished translation (``translations.json``, ``{pid: text}``), pid->pid
     1:1 against the chapter source. ZERO model calls: variants are Russian
     word counts over the translations of the pids whose source contains the
     candidate; a dominant variant whose share >= ``consensus_ratio`` becomes
     the ``target``, otherwise the candidate gets ``conflicts`` and no target.
     Term targets are additionally required to be unambiguous *across* the
     chapter (B9-F2 review PR #128): when two term candidates dominate on the
     same Russian variant, that variant is co-occurrence evidence for at most
     one of them — with no word-level alignment we cannot tell which, so both
     candidates lose the target (conservative, prevents unrelated co-occurring
     source terms from being promoted as the same unrelated target). B9-fix
     (t_800fedaf, dry-run run_005) adds two heuristics: term variants are
     ranked by candidate-specificity (``in_count / (1 + out_count)``) so
     chapter-wide collocations (получить/чувствовал/стороны) cannot outrank
     the candidate's own candidate-specific translation
     (преимущество/злость/блондинка), and a proper_name target equal to an
     established glossary VALUE of a different key is dropped as a
     co-occurring established name (Master -> Блэйк while Blake -> Блэйк).
  3. ``GlossaryCandidateLedger`` — append-only, line-based (one JSON object
     per line) accumulation into ``glossary_candidates.json``, v3-style merged
     records ``{source, kind, total_occurrences, chapters, variants, target,
     targets_seen, conflicts, first_context}``; the merged ``target`` is the
     single distinct non-None chapter target and is irreversible — once two
     chapters disagree it stays ``None`` forever and every distinct chapter
     target is emitted in ``conflicts``. A torn/partial trailing line (crash
     during append) is skipped on load instead of breaking it.

Constraints honoured (B9-I1 task card): no model calls, no HTTP, no side
effects on import; Phase 1/2, cascade, risk, journal schema, identity/cache
and ``MemoryManager`` are untouched — integration is B9-I2's job.

The B5 mixed-script allowlist itself (``bible_script_tokens`` /
``glossary_script_tokens`` / ``source_derived_allowlist``) lives in
``pact_v4._integrity_checks``; this module only *consumes* an already-built
allowlist (the runner computes it) so it stays self-contained and pure.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Tokenization / text helpers
# ---------------------------------------------------------------------------

# Latin word token: letters plus an optional single apostrophe contraction
# ("Blake's", "doesn't"). Hyphens split words apart; digits split tokens.
_WORD_TOKEN_RE = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)?")

# Cyrillic word token (translations are Russian).
_RU_WORD_RE = re.compile(r"[А-Яа-яЁё]+(?:['\u2019][А-Яа-яЁё]+)?")

# Capitalized Cyrillic word (first letter uppercase) — proper-name variants.
_RU_CAPITALIZED_RE = re.compile(r"[А-ЯЁ][а-яё]*(?:['\u2019][А-Яа-яЁё]+)?")

# Minimal HTML stripping for callers that pass raw chapter HTML instead of
# parsed text. Only used when the input actually contains tags; script/style
# bodies are removed first.
_SCRIPT_STYLE_RE = re.compile(r"<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)>", re.S | re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Characters that close a sentence (or an opening quote / dash) — a
# capitalized token right after one of these is sentence-initial, not a
# mid-sentence proper name.
_SENTENCE_END = ".!?\u2026;\u2014\u2013\u201c\u201d\u00ab\u00bb\u2018\u2019\"'"
_SOURCE_BOUNDARY = r"A-Za-z0-9_"

EN_STOP_WORDS: frozenset[str] = frozenset("""
a about above after again against all am an and any are aren't as at be
because been before being below between both but by can can't cannot could
couldn't did didn't do does doesn't doing don't down during each few for
from further had hadn't has hasn't have haven't having he he'd he'll he's
her here here's hers herself him himself his how how's i i'd i'll i'm i've
if in into is isn't it it's its itself let's me more most mustn't my myself
no nor not of off on once only or other ought our ours ourselves out over
own same shan't she she'd she'll she's should shouldn't so some such than
that that's the their theirs them themselves then there there's these they
they'd they'll they're they've this those through to too under until up very
was wasn't we we'd we'll we're we've were weren't what what's when when's
where where's which while who who's whom why why's with won't would wouldn't
you you'd you'll you're you've your yours yourself yourselves
already alone along always almost another anyone anything anywhere anyway
anymore everyone everything everywhere someone something somewhere somehow
sometime sometimes meanwhile anyway back away around
hey bye sorry listen look oh ah wow okay hello thanks nobody anybody
mr mrs ms dr st
""".split())

# Russian function words excluded from term-equivalent variant counts.
RU_STOP_WORDS: frozenset[str] = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за
бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну
вдруг ли если уже или ни быть был него до вас опять уж вам ведь там потом
себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам
чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому
этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас
были куда зачем никогда можно при наконец два об другой хоть после над
больше тот через эти нас про всего них какая много разве три эту моя
впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между нибудь собою очень однако причем притом либо
нежели ежели коль покуда доколе оттого откуда отколь
""".split())

# Capitalized Russian words that are sentence-start noise, not proper names.
# Single-letter words are excluded separately in the alignment code.
RU_CAPITALIZED_NOISE: frozenset[str] = frozenset("""
он она оно они это эта эти тот та те ты мы вы но что как кто где когда
если чтобы хотя потом теперь ну да ведь затем поэтому однако значит словно
будто вроде неужто неужели
""".split())

# Thresholds from the B9-I1 task card / V3 mechanics.
DEFAULT_MIN_NAME_OCCURRENCES = 2
DEFAULT_MIN_TERM_OCCURRENCES = 3
DEFAULT_MIN_TERM_LENGTH = 4
DEFAULT_CONSENSUS_RATIO = 0.8
# "заметно чаще" (noticeably more often) for a term-equivalent candidate:
# the word must appear in the term-pids at least this many times more often
# (rate ratio) than in the non-term pids.
DEFAULT_CONTRAST_RATIO = 2.0

LEDGER_VERSION = 1


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_html(text: str) -> str:
    """Return plain text from possibly-HTML input (best-effort, regex-only)."""
    if "<" not in text:
        return text
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    return text


def _escaped_term(term: str) -> str:
    """Word-boundary-safe, apostrophe-tolerant escaped regex for ``term``."""
    escaped = re.escape(term)
    escaped = escaped.replace(r"\ ", r"\s+")
    escaped = escaped.replace("'", "['\u2019]").replace("\u2019", "['\u2019]")
    return escaped


def _term_in_text(text: str, term: str) -> bool:
    """Case-insensitive word-boundary presence of ``term`` in ``text``."""
    return bool(re.search(
        rf"(?<![{_SOURCE_BOUNDARY}]){_escaped_term(term)}(?![{_SOURCE_BOUNDARY}])",
        text or "", flags=re.I,
    ))


def _is_sentence_start(prev: str) -> bool:
    """True if the text immediately before a token ends a sentence.

    Skips trailing whitespace (including non-breaking spaces) so a token
    after ``".\\xa0"`` or ``".  "`` is correctly treated as sentence-initial,
    while a token preceded only by a plain space (``" said Ivy"``) is not.
    """
    prev = prev.rstrip()
    return not prev or prev[-1] in _SENTENCE_END


def _extract_context(text: str, term: str) -> str:
    """First sentence of ``text`` containing ``term`` (deterministic)."""
    match = re.search(
        rf"(?<![{_SOURCE_BOUNDARY}]){_escaped_term(term)}(?![{_SOURCE_BOUNDARY}])",
        text or "", flags=re.I,
    )
    if not match:
        return ""
    start = match.start()
    end = match.end()
    before = text[:start]
    after = text[end:]
    s_start = max(before.rfind(ch) for ch in _SENTENCE_END) + 1 if before else 0
    s_end = len(text)
    for ch in _SENTENCE_END:
        idx = after.find(ch)
        if idx != -1:
            s_end = min(s_end, end + idx + 1)
    sentence = _norm(text[s_start:s_end])
    if len(sentence) > 300:
        sentence = sentence[:297].rstrip() + "..."
    return sentence


# ---------------------------------------------------------------------------
# Exclusion extraction (glossary keys, book-memory characters and variants)
# ---------------------------------------------------------------------------

def _glossary_terms(glossary: Any) -> List[str]:
    """Source-term strings from a v4 ``glossary.json`` (dict or list shape)."""
    terms: List[str] = []
    if isinstance(glossary, dict):
        items = list(glossary.items())
    elif isinstance(glossary, list):
        items = []
        for entry in glossary:
            if isinstance(entry, dict):
                source = entry.get("source_term") or entry.get("source")
                target = entry.get("target_terms") or entry.get("target")
                if source is not None and target is not None:
                    items.append((source, target))
    else:
        return terms
    for source, _target in items:
        terms.append(str(source))
    return terms


def _glossary_target_keys(glossary: Any) -> Dict[str, set]:
    """Casefolded glossary target VALUE -> set of source keys mapping to it.

    Mirror of ``_glossary_terms`` on the VALUE side, tolerant of both v4
    glossary shapes (flat dict ``{source: target}`` or list of
    ``{source_term/source, target_terms/target}``). Used by the B9-fix
    proper-name guard: a candidate whose aligned target equals the
    established translation of a DIFFERENT glossary key (``Master -> Блэйк``
    while the glossary already maps ``Blake -> Блэйк``) is aligning to a
    co-occurring established name, not to its own translation, and must not
    be promoted.
    """
    result: Dict[str, set] = {}
    if isinstance(glossary, dict):
        items = list(glossary.items())
    elif isinstance(glossary, list):
        items = []
        for entry in glossary:
            if isinstance(entry, dict):
                source = entry.get("source_term") or entry.get("source")
                target = entry.get("target_terms") or entry.get("target")
                if source is not None and target is not None:
                    items.append((source, target))
    else:
        return result
    for source, target in items:
        flat = str(target) if not isinstance(target, dict) else str(
            target.get("target") or ""
        )
        if flat:
            result.setdefault(flat.casefold(), set()).add(str(source).casefold())
    return result


_MEMORY_SECTIONS = ("characters", "entities", "terms")
_MEMORY_NAME_FIELDS = ("source", "english", "name", "term")


def _memory_terms(book_memory: Any) -> List[str]:
    """Character/entity names and their variants from ``book_memory.json``.

    Tolerant of both v4 shapes: a flat ``{term: entry}`` dict (dict keys are
    the names, each entry may carry a ``variants`` dict) and the v3
    sectioned bible shape (lists of dicts with ``source``/``english``/``name``
    fields). ``variants`` of an entry are treated as exclusion terms too —
    the B9-I1 card: "персонажи И variants из book_memory.json".
    """
    terms: List[str] = []
    if not isinstance(book_memory, dict):
        return terms
    if any(section in book_memory for section in _MEMORY_SECTIONS):
        for section in _MEMORY_SECTIONS:
            entries = book_memory.get(section)
            if isinstance(entries, dict):
                for key, entry in entries.items():
                    terms.append(str(key))
                    if isinstance(entry, dict):
                        variants = entry.get("variants")
                        if isinstance(variants, dict):
                            terms.extend(str(v) for v in variants.keys())
                        elif isinstance(variants, (list, tuple)):
                            terms.extend(str(v) for v in variants)
            elif isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        for key in _MEMORY_NAME_FIELDS:
                            value = entry.get(key)
                            if isinstance(value, str) and value:
                                terms.append(value)
    else:
        # Flat v4 book_memory: dict keys are the names, entries may carry
        # variants.
        for key, entry in book_memory.items():
            if isinstance(key, str) and key not in ("version", "pov"):
                terms.append(key)
            if isinstance(entry, dict):
                variants = entry.get("variants")
                if isinstance(variants, dict):
                    terms.extend(str(v) for v in variants.keys())
                elif isinstance(variants, (list, tuple)):
                    terms.extend(str(v) for v in variants)
    return terms


# ---------------------------------------------------------------------------
# 1. Candidate generator
# ---------------------------------------------------------------------------

def _normalize_source(source: Any) -> Tuple[Optional[List[Tuple[str, str]]], str]:
    """Normalize the generator input into (blocks, full_text).

    ``blocks`` is an ordered ``[(pid, text), ...]`` list when the caller
    supplied pid-level text (mapping, pid/text pairs, or ``SourceBlock``
    objects), else ``None`` for a plain chapter string. ``full_text`` is the
    complete plain text (HTML stripped) used for frequency counting.
    """
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
    # Preserve insertion order but keep the ordering deterministic for
    # duplicate PIDs by sorting on (pid, index-of-first-seen).
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


def generate_candidates(
    source: Any,
    *,
    glossary: Any = None,
    book_memory: Any = None,
    allowlist: Iterable[str] = (),
    pid_to_chunk: Optional[Mapping[str, str]] = None,
    min_name_occurrences: int = DEFAULT_MIN_NAME_OCCURRENCES,
    min_term_occurrences: int = DEFAULT_MIN_TERM_OCCURRENCES,
    min_term_length: int = DEFAULT_MIN_TERM_LENGTH,
) -> List[Dict[str, Any]]:
    """Deterministically scan a chapter's EN source for glossary candidates.

    ``source`` is the chapter source: a plain string (raw HTML or text), a
    ``{pid: text}`` mapping, a sequence of ``(pid, text)`` pairs, or a
    sequence of ``SourceBlock`` objects. ``pid_to_chunk`` maps pids to
    ``chunk_id`` so each candidate carries the chunks it appears in.

    Exclusions (B9-I1 card): established glossary keys, book-memory
    characters and their variants, and the B5 mixed-script allowlist tokens.
    ``allowlist`` is the already-combined token allowlist the runner computes
    (see ``pact_v4._integrity_checks``); pass an empty iterable when none.

    Returns a list of candidate records, deterministically ordered by
    (casefolded source, kind):

      ``{source, kind, occurrences, chunk_ids, context}``

    where ``kind`` is ``"proper_name"`` or ``"term"``, ``occurrences`` is the
    case-insensitive word-boundary count in the whole chapter, ``chunk_ids``
    the sorted unique chunks containing the term (empty when no pid-level
    input), and ``context`` an example sentence. No model calls, no I/O.
    """
    blocks, full_text = _normalize_source(source)
    if not full_text:
        return []

    exclude: set = set()
    exclude.update(t.casefold() for t in _glossary_terms(glossary))
    exclude.update(t.casefold() for t in _memory_terms(book_memory))
    exclude.update(_norm(str(item)).casefold()
                   for item in allowlist if _norm(str(item)))

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
            # Normalize the possessive suffix ("Blake's" counts as "Blake")
            # and skip contractions whose root is a function word ("I'd",
            # "she'll" — their prefix is a stop word).
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

    candidates: List[Dict[str, Any]] = []
    for folded, occurrences in counts.items():
        if occurrences < min_name_occurrences:
            continue
        if folded in exclude or folded in EN_STOP_WORDS:
            continue

        source_form = first_seen[folded]
        # A token that ever appears lowercase is a common noun/adjective
        # capitalized by sentence position — not a proper name.
        is_name = (folded in capitalized_mid and folded in has_title_case
                   and folded not in appears_lowercase)
        if is_name:
            kind = "proper_name"
        elif occurrences >= min_term_occurrences and len(
            re.findall(r"[A-Za-z]", folded)
        ) >= min_term_length:
            kind = "term"
        else:
            continue

        chunk_ids: List[str] = []
        if blocks is not None and pid_to_chunk:
            pids = tokens_in_blocks.get(folded, ())
            chunk_ids = sorted({pid_to_chunk[pid] for pid in pids
                                if pid in pid_to_chunk})

        candidates.append({
            "source": source_form,
            "kind": kind,
            "occurrences": occurrences,
            "chunk_ids": chunk_ids,
            "context": _extract_context(full_text, source_form),
        })

    candidates.sort(key=lambda c: (c["source"].casefold(), c["kind"]))
    return candidates


# ---------------------------------------------------------------------------
# 2. Consensus alignment (0 model calls)
# ---------------------------------------------------------------------------

# Russian case-ending stems, longest first — merges inflected forms of a word
# ("Блэйк" / "Блэйком" / "Блэйка") into one variant bucket for consensus
# counting, while the display form keeps the most frequent original spelling.
_RU_ENDINGS = sorted({
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
    "иях", "ах", "ях", "ой", "ей", "ый", "ий", "ая", "яя", "ую",
    "юю", "ом", "ем", "ым", "им", "ов", "ев", "ам", "ям", "а", "я",
    "у", "ю", "ы", "и", "е", "о", "ь",
}, key=len, reverse=True)


def _ru_stem(word: str) -> str:
    """Casefolded stem of a Russian word (endings stripped, >= 3 chars kept)."""
    core = re.sub(r"[^А-Яа-яЁё]", "", word).casefold().replace("ё", "е")
    if len(core) <= 2:
        return core
    for ending in _RU_ENDINGS:
        if core.endswith(ending) and len(core) - len(ending) >= 3:
            return core[:-len(ending)]
    return core


def _capitalized_ru_words(text: str) -> List[str]:
    """Capitalized Cyrillic words, minus sentence-start noise words."""
    result: List[str] = []
    for token in _RU_CAPITALIZED_RE.findall(text or ""):
        word = token.casefold()
        if len(word) < 2:
            continue
        if word in RU_CAPITALIZED_NOISE:
            continue
        result.append(token)
    return result


def _ru_words(text: str) -> List[str]:
    """All Cyrillic words, casefolded, minus stop-words and short particles."""
    result: List[str] = []
    for token in _RU_WORD_RE.findall(text or ""):
        word = token.casefold()
        if len(word) < 3:
            continue
        if word in RU_STOP_WORDS:
            continue
        result.append(word)
    return result


def _lowercase_ru_stems(text: str) -> set:
    """Stems of Cyrillic words that start lowercase in the original text."""
    stems: set = set()
    for token in _RU_WORD_RE.findall(text or ""):
        if token[0].islower():
            stems.add(_ru_stem(token))
    return stems


def _ordered_unique(words: Iterable[str]) -> List[str]:
    """Deduplicate preserving first-seen order (deterministic, no set())."""
    return list(dict.fromkeys(words))


def _pick_display_form(stem: str, forms: Dict[str, int]) -> str:
    """Most frequent original form for a stem; ties by count, then alphabet."""
    return max(sorted(forms), key=lambda form: (forms[form], form))


def align_candidates(
    candidates: Sequence[Mapping[str, Any]],
    source_by_pid: Mapping[str, str],
    translations: Mapping[str, str],
    *,
    consensus_ratio: float = DEFAULT_CONSENSUS_RATIO,
    contrast_ratio: float = DEFAULT_CONTRAST_RATIO,
    glossary: Any = None,
) -> List[Dict[str, Any]]:
    """Align candidate targets against a finished chapter translation.

    v4 pid->pid 1:1: ``source_by_pid`` is the chapter source ``{pid: text}``
    (from ``parse_source_html``), ``translations`` the finished
    ``translations.json`` ``{pid: text}``.

    For every candidate: the pids whose source contains the term are matched
    to their translations, and Russian-equivalent variants are counted
    (pid-presence — one count per pid per variant, Russian inflections
    merged by stem):

      * ``proper_name``: capitalized Cyrillic words in those translations,
        excluding common nouns that only get a capital from sentence
        position (their stem also occurs in lowercase). The variant keeps
        the most frequent original capitalized spelling (e.g. ``Блэйк``,
        not the inflected ``Блэйком``);
      * ``term``: Cyrillic words whose rate among term-pids is at least
        ``contrast_ratio`` times their rate among the other pids (frequency
        contrast) and that appear in >= 2 term-pids.

    If the dominant variant's share of the examined pids is >=
    ``consensus_ratio`` it becomes ``target``; otherwise ``conflicts`` lists
    all competing variants and there is no target.

    Two B9-fix heuristics (t_800fedaf, offline validation of run_005) keep
    the dominant honest:

      * ``term`` variants are ranked by candidate-specificity
        (``in_count / (1 + out_count)``, where ``out_count`` is how many
        NON-matching pids the variant appears in), NOT by raw pid-presence.
        A collocation/common word that also shows up all over the chapter
        (``получить``, ``чувствовал``, ``стороны``) no longer outranks the
        candidate's own candidate-specific translation (``преимущество``,
        ``злость``, ``блондинка``) — without this, ``advantage -> получить``
        / ``anger -> чувствовал`` / ``blonde -> стороны`` would auto-promote;
      * a ``proper_name`` target that equals an established glossary VALUE of
        a DIFFERENT key (``Master -> Блэйк`` while the glossary already maps
        ``Blake -> Блэйк``) is a co-occurring established name, not this
        candidate's own translation — the target is dropped (conflict), so
        the pair can never promote.

    ``glossary`` (optional, the established ``glossary.json``) powers the
    second guard; without it (``None``) that guard is a no-op.

    A term target is only kept when it is unambiguous *within the chapter*
    (B9-F2 review PR #128): if two ``term`` candidates both dominate on the
    same Russian variant, that variant is co-occurrence evidence for at most
    one of them and the frequency-contrast heuristic cannot tell which, so
    BOTH candidates lose the target and the shared variant lands in their
    ``conflicts``. Without this rule an unrelated co-occurring source term
    (e.g. ``bound`` next to ``pact`` in the same sentences) could be
    auto-promoted as the same unrelated target (e.g. ``пакт``).

    Returns a list of aligned
    records (input candidate fields plus ``matching_pid_count``,
    ``variants``, ``target``, ``consensus_share``, ``conflicts``).
    Deterministic; no model calls.
    """
    aligned: List[Dict[str, Any]] = []
    # Casefolded established glossary VALUE -> its source keys (B9-fix guard
    # for proper_name targets that collide with another key's translation).
    glossary_target_keys = _glossary_target_keys(glossary)
    for candidate in candidates:
        source = str(candidate.get("source") or "")
        if not source:
            continue
        matching = [pid for pid, text in source_by_pid.items()
                    if _term_in_text(text, source)]
        examined = [pid for pid in matching if pid in translations]
        if not examined:
            aligned.append({
                **candidate,
                "matching_pid_count": len(examined),
                "variants": {},
                "target": None,
                "consensus_share": 0.0,
                "conflicts": [],
            })
            continue

        if candidate.get("kind") == "proper_name":
            # Common-noun filter: a capitalized word whose stem also occurs
            # in lowercase anywhere in the chapter's translations is
            # capitalized only by sentence position, not a proper name.
            # The lowercase evidence is chapter-wide (every translated pid,
            # matching or not): a sentence-initial "Дом" in the
            # candidate-matching pids must not become a proper-name target
            # when "дом" occurs lowercase in a different pid of the same
            # chapter (P2 review finding).
            lowercase_stems: set = set()
            for pid, text in translations.items():
                lowercase_stems |= _lowercase_ru_stems(text)
            stem_forms: Dict[str, Dict[str, int]] = {}
            stem_order: List[str] = []
            stem_pids: Dict[str, List[str]] = {}
            for pid in examined:
                seen_this_pid: set = set()
                for word in _ordered_unique(_capitalized_ru_words(translations[pid])):
                    stem = _ru_stem(word)
                    if stem in lowercase_stems:
                        continue
                    if stem not in stem_forms:
                        stem_forms[stem] = {}
                        stem_order.append(stem)
                        stem_pids[stem] = []
                    stem_forms[stem][word] = stem_forms[stem].get(word, 0) + 1
                    if stem not in seen_this_pid:
                        seen_this_pid.add(stem)
                        stem_pids[stem].append(pid)
            variant_counts = {
                _pick_display_form(stem, forms): len(stem_pids[stem])
                for stem, forms in stem_forms.items()
            }
            # Deterministic ordering: count desc, then first-seen order,
            # then alphabetical.
            ordered_keys = sorted(
                variant_counts,
                key=lambda w: (-variant_counts[w],
                               stem_order.index(_ru_stem(w)), w),
            )
        else:
            other_pids = [pid for pid, text in source_by_pid.items()
                          if pid not in matching and pid in translations]
            other_counts: Dict[str, int] = {}
            for pid in other_pids:
                for stem in {_ru_stem(w) for w in
                             _ordered_unique(_ru_words(translations[pid]))}:
                    other_counts[stem] = other_counts.get(stem, 0) + 1
            stem_forms = {}
            stem_order = []
            stem_pids = {}
            for pid in examined:
                seen_this_pid = set()
                for word in _ordered_unique(_ru_words(translations[pid])):
                    stem = _ru_stem(word)
                    if stem not in stem_forms:
                        stem_forms[stem] = {}
                        stem_order.append(stem)
                        stem_pids[stem] = []
                    stem_forms[stem][word] = stem_forms[stem].get(word, 0) + 1
                    if stem not in seen_this_pid:
                        seen_this_pid.add(stem)
                        stem_pids[stem].append(pid)
            variant_counts = {}
            for stem in stem_order:
                in_count = len(stem_pids[stem])
                if in_count < 2:
                    continue
                in_rate = in_count / len(examined)
                out_rate = (other_counts.get(stem, 0) / len(other_pids)
                            if other_pids else 0.0)
                if out_rate and in_rate / out_rate < contrast_ratio:
                    continue
                display = _pick_display_form(stem, stem_forms[stem])
                variant_counts[display] = in_count
            # B9-fix (t_800fedaf, audit HIGH 2): rank term variants by
            # candidate-specificity, not raw pid-presence. A variant that
            # ALSO appears in many non-matching pids is a collocation/common
            # word, not the candidate's own translation (advantage->получить,
            # anger->чувствовал, blonde->стороны: получить/чувствовал/
            # стороны show up all over the chapter, while the true
            # translations преимущество/злость/блондинка are confined to the
            # matching pids). Score = in_count / (1 + out_count); ties by
            # in_count desc, then first-seen, then alphabetical
            # (deterministic).
            out_count = {stem: other_counts.get(stem, 0) for stem in stem_order}
            ordered_keys = sorted(
                variant_counts,
                key=lambda w: (-(variant_counts[w]
                                 / (1 + out_count[_ru_stem(w)])),
                               -variant_counts[w],
                               stem_order.index(_ru_stem(w)), w),
            )

        variants = {w: variant_counts[w] for w in ordered_keys}
        dominant = ordered_keys[0] if ordered_keys else None
        if (dominant is not None and candidate.get("kind") == "proper_name"
                and dominant.casefold() in glossary_target_keys
                and source.casefold()
                not in glossary_target_keys[dominant.casefold()]):
            # B9-fix (t_800fedaf, audit HIGH 1): the aligned target is the
            # established glossary VALUE of a DIFFERENT key (Master -> Блэйк
            # while the glossary already maps Blake -> Блэйк) — a
            # co-occurring established name, not this candidate's own
            # translation. Drop the target: the wrong pair must never
            # promote (and never reach the ledger).
            dominant = None
        share = (variant_counts[dominant] / len(examined)) if dominant else 0.0
        if dominant is not None and share >= consensus_ratio:
            target: Optional[str] = dominant
            conflicts: List[str] = []
        else:
            target = None
            conflicts = ordered_keys

        aligned.append({
            **candidate,
            "matching_pid_count": len(examined),
            "variants": variants,
            "target": target,
            "consensus_share": round(share, 6),
            "conflicts": conflicts,
        })

    # B9-F2 (review PR #128): term targets must be unambiguous across the
    # chapter. The frequency-contrast heuristic cannot distinguish "the
    # candidate's translation" from "a word that merely co-occurs with the
    # candidate in the same pids" — when two term candidates of this chapter
    # both dominate on the SAME Russian variant, that variant is credible
    # evidence for at most one of them, and without word-level alignment we
    # cannot tell which. Conservative: neither candidate keeps it as a target
    # (both become conflicts), so unrelated co-occurring source terms can
    # never be promoted as the same unrelated target.
    claims: Dict[str, int] = {}
    for record in aligned:
        if record.get("kind") == "term" and record.get("target") is not None:
            target_s = str(record["target"])
            claims[target_s] = claims.get(target_s, 0) + 1
    for record in aligned:
        if record.get("kind") != "term" or record.get("target") is None:
            continue
        if claims.get(str(record["target"]), 0) > 1:
            record["target"] = None
            record["consensus_share"] = 0.0
            record["conflicts"] = list(record["variants"].keys())
    return aligned


# ---------------------------------------------------------------------------
# 3. Append-only ledger (glossary_candidates.json)
# ---------------------------------------------------------------------------

def candidate_key(source: str, kind: str) -> str:
    """Stable ledger key for a candidate: ``kind|casefolded source``."""
    return f"{kind}|{source.casefold()}"


class GlossaryCandidateLedger:
    """Append-only, line-based candidate accumulator.

    ``glossary_candidates.json`` holds one JSON object per line — a chapter
    observation for one candidate. Loading parses complete lines and merges
    them by ``candidate_key`` into v3-style records:

      ``{source, kind, total_occurrences,
        chapters: [{chapter_id, chunk_ids, count}],
        variants: {variant: count}, target, targets_seen, conflicts,
        first_context}``

    ``target`` is the single distinct non-None per-chapter target and is
    irreversible: once two chapters disagree it stays ``None`` forever (a
    later chapter matching the first target does not resurrect it) and the
    disagreeing chapter targets are emitted in ``conflicts``.
    ``targets_seen`` is the running list of distinct non-None chapter
    targets (first-seen order) that backs that decision.

    Crash-safety: a torn partial *trailing* line (interrupted append) is
    skipped on load; a corrupt line in the middle raises ``ValueError``.
    Re-observing the same (chapter, candidate) updates that chapter's line in
    place (idempotent re-runs); new chapters are appended. Nothing is ever
    deleted.
    """

    def __init__(self, path: str):
        self.path = path

    # -- reading -----------------------------------------------------------

    def load(self) -> Dict[str, Dict[str, Any]]:
        """Merged records keyed by ``candidate_key(source, kind)``."""
        observations = self._load_lines()
        return self.merge({}, observations)

    def _read_raw_lines(self) -> List[str]:
        """Raw lines of the ledger file (empty list when absent)."""
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
                    # Torn trailing line from an interrupted append — ignore.
                    continue
                raise ValueError(
                    f"Corrupt glossary candidate ledger line {index + 1} in "
                    f"{self.path}: {exc}"
                ) from exc
        return observations

    # -- merging (pure) ----------------------------------------------------

    @staticmethod
    def merge(
        left: Mapping[str, Any], right: Any,
    ) -> Dict[str, Dict[str, Any]]:
        """Merge two observation sources into merged records.

        ``left`` is a ``{candidate_key: observation_or_records}`` mapping;
        ``right`` is either the same shape or a plain sequence of observation
        dicts (one per chapter line, as ``load()`` produces). Observations are
        first collapsed per ``(candidate_key, chapter_id)`` — the last
        observation wins — so merging overlapping snapshots (e.g. two loads of
        the same file) is idempotent and never double-counts a chapter. Per
        candidate the surviving observations are then accumulated:
        ``total_occurrences`` sums, ``chapters`` entries are appended,
        ``variants`` counts sum, ``first_context`` keeps the earliest, and the
        merged ``target`` is the single distinct non-None per-chapter target.
        The decision is irreversible: once two chapters disagree, ``target``
        stays ``None`` forever (a later chapter matching the first target
        does not resurrect it) and every distinct chapter target is emitted
        in ``conflicts`` alongside the per-chapter conflicts.
        """
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
            merged[key] = GlossaryCandidateLedger._merge_one(
                merged.get(key), item
            )
        return merged

    @staticmethod
    def _merge_one(
        record: Optional[Dict[str, Any]], obs: Dict[str, Any],
    ) -> Dict[str, Any]:
        source = str(obs.get("source") or "")
        kind = str(obs.get("kind") or "term")
        occurrences = int(obs.get("occurrences") or 0)
        chapter_id = str(obs.get("chapter_id") or "")
        chunk_ids = sorted({str(c) for c in (obs.get("chunk_ids") or [])})
        context = str(obs.get("context") or "")
        variants = obs.get("variants") if isinstance(obs.get("variants"), dict) else {}
        target = obs.get("target")
        conflicts = [str(c) for c in (obs.get("conflicts") or [])]

        if record is None:
            record = {
                "source": source,
                "kind": kind,
                "total_occurrences": 0,
                "chapters": [],
                "variants": {},
                "target": None,
                "conflicts": [],
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
                replaced = True
                break
        if not replaced and chapter_id:
            chapters.append({"chapter_id": chapter_id,
                             "chunk_ids": chunk_ids, "count": occurrences})
        record["chapters"] = chapters

        merged_variants = dict(record.get("variants") or {})
        for word, count in variants.items():
            merged_variants[str(word)] = merged_variants.get(str(word), 0) + int(count)
        record["variants"] = dict(sorted(
            merged_variants.items(), key=lambda kv: (-kv[1], kv[0])
        ))

        record["conflicts"] = sorted(set(record.get("conflicts") or []) | set(conflicts))
        record["first_context"] = record.get("first_context") or context
        distinct_targets = record.get("targets_seen")
        if not isinstance(distinct_targets, list):
            # Foreign/legacy record without the tracking field — seed it
            # from the existing target so the consensus stays irreversible
            # from this point on.
            distinct_targets = []
            if record.get("target") is not None:
                distinct_targets.append(str(record["target"]))
            record["targets_seen"] = distinct_targets
        record["target"] = GlossaryCandidateLedger._merge_target(
            distinct_targets, target
        )
        if len(distinct_targets) > 1:
            # Cross-chapter disagreement: every distinct chapter target is a
            # competing variant — emit them all in conflicts without dropping
            # the per-chapter conflicts already recorded.
            record["conflicts"] = sorted(
                set(record["conflicts"]) | set(distinct_targets)
            )
        return record

    @staticmethod
    def _merge_target(targets_seen, incoming) -> Optional[str]:
        """Merged target for a record, irreversible once chapters disagree.

        ``targets_seen`` is the record's running list of distinct non-None
        per-chapter targets (first-seen order). It is updated in place with
        ``incoming``; the merged target is the single distinct value, or
        ``None`` forever once two different chapter targets have been seen.
        A later chapter matching the first target must NOT resurrect it —
        the disagreement is already recorded in ``conflicts`` (P1 review
        finding: targets Альфа, Бета, Альфа must stay without a target).
        """
        if incoming is not None:
            incoming_s = str(incoming)
            if incoming_s not in targets_seen:
                targets_seen.append(incoming_s)
        if len(targets_seen) == 1:
            return targets_seen[0]
        return None

    # -- writing -----------------------------------------------------------

    def append_chapter(
        self, chapter_id: str, aligned_candidates: Sequence[Mapping[str, Any]],
    ) -> Dict[str, int]:
        """Append one line per aligned candidate for ``chapter_id``.

        Returns stats ``{appended, new_candidates, updated}``. A candidate
        whose (chapter_id, key) line already exists is updated in place
        (idempotent re-run) instead of duplicating; brand-new lines are
        appended at the end. Writing is crash-safe: the file is either
        appended line-by-line or rewritten atomically (temp + rename).
        """
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
                    # Torn trailing line from an interrupted append —
                    # dropped before rewriting.
                    torn_trailing = index
                    continue
                raise
            key = candidate_key(str(obs.get("source") or ""),
                                str(obs.get("kind") or ""))
            if str(obs.get("chapter_id")) == chapter_id:
                line_by_key[key] = index

        stats = {"appended": 0, "new_candidates": 0, "updated": 0}
        for candidate in aligned_candidates:
            source = str(candidate.get("source") or "")
            kind = str(candidate.get("kind") or "term")
            if not source:
                continue
            key = candidate_key(source, kind)
            obs = {
                "chapter_id": chapter_id,
                "source": source,
                "kind": kind,
                "occurrences": int(candidate.get("occurrences") or 0),
                "chunk_ids": sorted({str(c) for c in (candidate.get("chunk_ids") or [])}),
                "context": str(candidate.get("context") or ""),
                "variants": candidate.get("variants") or {},
                "target": candidate.get("target"),
                "conflicts": [str(c) for c in (candidate.get("conflicts") or [])],
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
            # No new line absorbed it — drop the torn line so the file stays
            # clean (an updated-only run must not keep the partial line).
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
