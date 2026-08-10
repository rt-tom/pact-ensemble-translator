"""B1.1: Tier A hard deterministic filters for audit findings (0 model calls).

Canonical source: ``docs/plans/V4_1_AUDIT_B1_RU.md`` §5.1/§5.3 and §10
(B1.1). Runs on the issues produced by the chunked Qwen audit (B1, format
fixed in ``audit_v4.ps1``: ``id/category/severity/confidence/note/excerpt/
_debug``) BEFORE repair, and decides for every issue exactly one of:

* ``CONFIRMED`` — Tier A: the issue is deterministically proven (exact
  adjacent duplicate, explicit number/time mismatch against the source).
  §5.3: Tier A findings repair directly.
* ``REJECTED`` — Tier A false positive: deterministically refuted (numbers/
  times match the source *in text order*; a source quoted string is
  preserved verbatim in the translation; the translation follows an
  explicit current-source gender fact). Dropped, never repaired.
* ``TIER_B`` — needs semantic verification (B2 repair-as-verifier). This is
  the default for anything the hard filters cannot decide, and it is
  *forced* for chapter entity relations.

Tier A scope (§5.1): structure (PID missing/outside chunk, invalid
category), exact numbers/time (normalization: ``Two past twelve`` = 00:02,
Russian ``девяти/десяти`` word forms), exact adjacent duplicates
(``в гости в гости``), explicit names/strings, and direct current-source
facts (an explicit number/name/object in the *current* source pair — no
semantic edge).

Deterministic contracts (RV t_ac2fb507 fixes, 2026-08-10):

1. **Numbers/time are compared POSITIONALLY (text order), never as a
   sorted multiset.** Identical values in identical order -> ``REJECTED``
   (FP). The *same multiset in a different order* (e.g. ``Two cats and
   three dogs`` vs ``Три кошки и две собаки``) -> ``TIER_B``: which number
   belongs to which fact cannot be proven without semantics, so it is never
   ``REJECTED`` (and never ``CONFIRMED``). Only a genuinely different
   multiset -> ``CONFIRMED``.

2. **Source gender evidence is fail-safe and deliberately narrow.** Only
   EN subject pronouns (``he``/``she``) and reflexive pronouns
   (``himself``/``herself``) count as source evidence. Object pronouns
   (``him``/``her``), possessive forms (``his``/``hers``) and role/status
   nouns (``man``, ``male``, ``brother``, ``mr``, ...) are NOT evidence:
   they can name a *different* participant (``The nurse spoke to him`` —
   ``him`` is not the nurse; ``His brother was a nurse``). Without entity
   resolution (B1.2) their attachment to the target entity is unproven, so
   the issue stays ``TIER_B``. Residual coreference ambiguity of a subject
   pronoun is accepted and documented; full resolution is B1.2 scope.

3. **Explicit strings (§5.1 names/strings): REJECT-only, verbatim
   preservation.** If the current source pair contains an explicitly quoted
   string (double quotes or guillemets) and that exact string appears
   verbatim in the translation, a changed_fact/addition/omission claim
   about it is a deterministic FP -> ``REJECTED``. A translated,
   transliterated or absent variant is NEVER ``CONFIRMED`` (that would be a
   semantic edge) — it stays ``TIER_B``, as do unquoted names/objects
   (they need entity resolution).

CRITICAL (§5.3, card rule 5): ``chapter_entity_context`` is NEVER Tier A.
An issue whose PID participates in the chapter entity context (anchor/alias
of any claim) or whose note references chapter entity facts is ALWAYS
``TIER_B`` — the presence of verified anchor/alias spans does NOT promote a
relation (``bike = motorcycle``) to Tier A. The relation itself is a
semantic claim.

This module is pure and stateless: no model calls, no disk I/O, no
provenance/identity handling. Deterministic — same inputs always yield the
same verdicts.

Explicitly NOT implemented here: semantic verification (Tier B — B2),
repair, entity extraction (B1.2), any prompt change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Collection, Mapping, Sequence, Tuple

__all__ = [
    "CONFIRMED",
    "REJECTED",
    "TIER_B",
    "B1_AUDIT_CATEGORIES",
    "B1_SEVERITIES",
    "B1_CONFIDENCES",
    "FilteredIssue",
    "apply_hard_filters",
    "find_adjacent_duplicate",
    "normalized_numeric_values",
]

CONFIRMED = "confirmed"
REJECTED = "rejected"
TIER_B = "tier_b"

# B1 issue-format contract (audit_v4.ps1 / B1 card).
B1_AUDIT_CATEGORIES = frozenset(
    {"omission", "addition", "referent", "invented_gender", "changed_fact", "negation"}
)
B1_SEVERITIES = frozenset({"major", "minor"})
B1_CONFIDENCES = frozenset({"high", "medium", "low"})

# Categories where a number/time claim is plausible enough to run the
# numeric normalizer against (an invented_gender or referent finding is not
# a numeric claim even if its note happens to contain a digit).
_NUMERIC_CATEGORIES = frozenset({"changed_fact", "addition", "omission", "negation"})
# Categories where an explicit current-source gender fact can refute the
# finding deterministically.
_GENDER_CATEGORIES = frozenset({"invented_gender", "referent"})
# Categories where an explicit quoted-string fact can refute the finding
# deterministically (a string/name/object was claimed changed, added or
# omitted). Negation and referent/invented_gender are not string claims.
_STRING_CATEGORIES = frozenset({"changed_fact", "addition", "omission"})

# --- number words (plain values; time-only words live in the time tables) ---

_EN_PLAIN_NUMBERS: dict = {
    "zero": 0,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}

_RU_PLAIN_NUMBERS: dict = {
    "ноль": 0, "нуль": 0,
    "один": 1, "одна": 1, "одно": 1, "одну": 1, "одного": 1, "одной": 1,
    "два": 2, "две": 2, "двух": 2, "двум": 2, "двумя": 2,
    "три": 3, "трёх": 3, "трех": 3, "трем": 3, "трём": 3, "тремя": 3,
    "четыре": 4, "четырёх": 4, "четырех": 4,
    "пять": 5, "пяти": 5,
    "шесть": 6, "шести": 6,
    "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8,
    "девять": 9, "девяти": 9,
    "десять": 10, "десяти": 10,
    "одиннадцать": 11, "одиннадцати": 11,
    "двенадцать": 12, "двенадцати": 12,
    "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14,
    "пятнадцать": 15, "пятнадцати": 15,
    "шестнадцать": 16, "шестнадцати": 16,
    "семнадцать": 17, "семнадцати": 17,
    "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19,
    "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100,
}

# Russian genitive ordinals used in the "минут N-го" time construction:
# "две минуты первого" = 00:02, "десять минут одиннадцатого" = 10:10.
_RU_ORDINAL_HOURS: dict = {
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4,
    "четвёртого": 4, "пятого": 5, "шестого": 6, "седьмого": 7,
    "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11,
    "двенадцатого": 12,
}

_EN_TIME_WORDS: dict = dict(_EN_PLAIN_NUMBERS, half=30, quarter=15)
_RU_TIME_WORDS: dict = dict(_RU_PLAIN_NUMBERS, половина=30, четверть=15, четверти=15)

# --- explicit gender markers (current-source facts only, no semantics) ---
#
# EN SOURCE evidence — FAIL-SAFE contract (RV t_ac2fb507 fix): only
# subject pronouns (he/she) and reflexive pronouns (himself/herself) count.
# These are grammatically bound to the clause subject — the entity being
# described. Object pronouns (him/her), possessive forms (his/hers) and
# role/status nouns (man, male, boy, brother, father, ..., mr / woman,
# female, girl, sister, mother, ..., mrs/ms/miss) are NOT source evidence:
# without entity resolution (B1.2) they cannot be proven to refer to the
# target entity (``The nurse spoke to him`` — ``him`` is another
# participant; ``His brother was a nurse`` — ``his`` is the sibling's).
# When only such markers are present the source gender is "unknown" and the
# issue stays TIER_B (§5.1 fail-safe).
_EN_MALE_RE = re.compile(r"\b(?:he|himself)\b", re.I)
_EN_FEMALE_RE = re.compile(r"\b(?:she|herself)\b", re.I)
_RU_MALE_RE = re.compile(
    r"\b(?:он|медбрат|медбрата|мужчина|мужчины|мужчин|брат|брата|отец|"
    r"сын|парень|юноша|дед)\b",
    re.I,
)
_RU_FEMALE_RE = re.compile(
    r"\b(?:она|медсестра|медсестры|медсестру|женщина|женщины|женщин|"
    r"сестра|сестры|мать|дочь|девушка|бабушка)\b",
    re.I,
)

# --- chapter entity context: note markers (lowercased substring match) ---

_ENTITY_CONTEXT_NOTE_MARKERS = (
    "chapter entity",
    "entity context",
    "entity fact",
    "chapter context",
    "blake's motorcycle",
    "blake's bike",
    "blake's vehicle",
    "refers to a motorcycle",
    "refer to a motorcycle",
    "per chapter context",
    "chapter facts",
    "established chapter",
)

# Time-expression patterns, checked before plain-number extraction so the
# words/digits of a time expression are not counted twice. Every match is
# blanked out of the text before plain numbers are read from the remainder.
# Russian ordinal hours are matched as full words (``первого`` — no
# hyphen); the ``минут N-го`` construction is ``две минуты первого``.
_EN_TIME_PATTERNS = (
    (re.compile(r"\b(\d{1,2}):(\d{2})\b"), "digit"),
    (re.compile(r"\b(\w+) past (\w+)\b"), "past"),
    (re.compile(r"\b(\w+) to (\w+)\b"), "to"),
    (re.compile(r"\b(\w+) o'clock\b"), "oclock"),
)
_RU_TIME_PATTERNS = (
    (re.compile(r"\b(\d{1,2}):(\d{2})\b"), "digit"),
    (re.compile(r"\b(\w+) минут\w* (\w+)\b"), "minutes_of"),
    (re.compile(r"\bполовина (\w+)\b"), "half_of"),
    (re.compile(r"\bчетверть (\w+)\b"), "quarter_of"),
    (re.compile(r"\bбез (\w+) (\w+)\b"), "to"),
    (re.compile(r"\b(\w+) час\w*\b"), "oclock"),
)

_NOTE_NUMERIC_HINT_RE = re.compile(
    r"\d|"                              # any digit (incl. 12:58, 1:02)
    r"\bo'?clock\b|"
    r"\bминут\w*\b|\bчас(?:а|ов|у)?\b|"
    r"\bполовина\b|\bчетверть\b|\bполчаса\b"
)


def _hour_24(hour: int) -> int:
    """Map a 12/24-hour clock hour to minutes-of-day hour (12-hour -> 0..11).

    ``Two past twelve`` = 00:02 and ``две минуты первого`` = 00:02 both
    normalize to hour 0, so a faithful translation of either renders the
    other identically. A 24-hour digit form (``13:02``) maps onto the same
    clock face as its Russian word rendering (``две минуты второго`` =
    1:02), so hour 13 -> 1. Hours >= 24 are kept as written (never occurs
    in fiction time expressions).
    """
    if 1 <= hour <= 23:
        return hour % 12
    return hour


def _minutes(direction: str, minute_word: str, hour_word: str, *, lang: str) -> int | None:
    """Canonical minutes-of-day for a ``M past/to H`` expression, or None.

    ``past``/``to`` operate on the 12-hour clock face before mapping to
    minutes-of-day: ``two past twelve`` = 00:02, ``five to three`` = 2:55,
    ``two to twelve`` = 11:58. The hour for ``to`` is used raw (12 stays
    12, so ``two to twelve`` lands on the *previous* hour), and only the
    ``past`` result passes through ``_hour_24`` (so ``two past twelve`` and
    Russian ``две минуты первого`` both normalize to 00:02).
    """
    table = _EN_TIME_WORDS if lang == "en" else _RU_TIME_WORDS
    minute = table.get(minute_word.lower())
    if lang == "ru":
        hour = _RU_ORDINAL_HOURS.get(hour_word.lower())
        if hour is None:
            hour = table.get(hour_word.lower())
    else:
        hour = table.get(hour_word.lower())
    if minute is None or hour is None:
        return None
    if direction == "past":
        return _hour_24(hour) * 60 + minute
    # "to": five to three = 2:55, two to twelve = 11:58
    return (hour - 1) * 60 + (60 - minute)


def _blanked(text: str, spans: Collection[Tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def _extract_times(text: str, lang: str) -> Tuple[Tuple[int, ...], str]:
    """Extract normalized minute-of-day values from time expressions.

    Returns ``(values, remaining_text)`` where ``values`` are in **text
    order** (sorted by match position, not by value) and every matched time
    expression has been blanked out of ``remaining_text`` so plain-number
    extraction does not double-count its words/digits. Patterns run on a
    working copy that is blanked after each pattern, so overlapping matches
    (e.g. ``half past ten o'clock``) never count twice.
    """
    patterns = _EN_TIME_PATTERNS if lang == "en" else _RU_TIME_PATTERNS
    found: list = []  # (match_start, value) — text-order tracking
    working = text
    for pattern, kind in patterns:
        spans: list = []
        for match in pattern.finditer(working):
            value: int | None = None
            if kind == "digit":
                hour, minute = int(match.group(1)), int(match.group(2))
                value = _hour_24(hour) * 60 + minute
            elif kind in ("past", "to"):
                value = _minutes(
                    kind, match.group(1), match.group(2), lang=lang
                )
            elif kind == "minutes_of":
                # "две минуты первого": minute=две, hour ordinal=первого(1) -> 00:02
                minute = _RU_TIME_WORDS.get(match.group(1).lower())
                hour = _RU_ORDINAL_HOURS.get(match.group(2).lower())
                if minute is not None and hour is not None:
                    value = (hour - 1) * 60 + minute
            elif kind == "half_of":
                hour = _RU_ORDINAL_HOURS.get(match.group(1).lower())
                if hour is not None:
                    value = (hour - 1) * 60 + 30
            elif kind == "quarter_of":
                hour = _RU_ORDINAL_HOURS.get(match.group(1).lower())
                if hour is not None:
                    value = (hour - 1) * 60 + 15
            elif kind == "oclock":
                table = _EN_TIME_WORDS if lang == "en" else _RU_TIME_WORDS
                hour = table.get(match.group(1).lower())
                if hour is not None:
                    value = _hour_24(hour) * 60
            if value is not None:
                found.append((match.start(), value))
                spans.append(match.span())
        if spans:
            working = _blanked(working, spans)
    found.sort(key=lambda item: item[0])
    return tuple(value for _, value in found), working


def _ordered_numeric_values(text: str, lang: str) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """Numeric content of ``text`` in **text order**: ``(times, numbers)``.

    Unlike :func:`normalized_numeric_values` (which returns sorted canonical
    multisets), this preserves the order in which the values appear, so the
    comparison can detect that ``Two cats and three dogs`` and ``Три кошки
    и две собаки`` carry the *same* quantities but attached to *different*
    facts — a correspondence the sorted multiset comparison cannot see.
    """
    times, remaining = _extract_times(text, lang)
    table = _EN_PLAIN_NUMBERS if lang == "en" else _RU_PLAIN_NUMBERS
    numbers: list = []
    for token in re.findall(r"\d+(?:[.,]\d+)?|[A-Za-zА-Яа-яЁё]+", remaining):
        if token[0].isdigit():
            numbers.append(int(float(token.replace(",", "."))))
            continue
        value = table.get(token.lower())
        if value is not None:
            numbers.append(value)
    return times, tuple(numbers)


def normalized_numeric_values(text: str, lang: str) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """Canonical numeric content of ``text``: ``(times, plain_numbers)``.

    ``times`` are minute-of-day values from recognized time expressions
    (``Two past twelve`` = (2,), ``две минуты первого`` = (2,),
    ``половина одиннадцатого`` = (630,)); ``plain_numbers`` are the digits
    and number words *outside* time expressions (``десяти`` -> 10,
    ``двадцать`` -> 20). Both tuples are returned as sorted canonical
    multisets (order-insensitive); use :func:`_ordered_numeric_values` when
    the *positional* correspondence between source and translation matters.
    Deterministic and language-tagged (``"en"`` for the source, ``"ru"``
    for the translation).
    """
    times, numbers = _ordered_numeric_values(text, lang)
    return tuple(sorted(times)), tuple(sorted(numbers))


def find_adjacent_duplicate(text: str) -> Tuple[str, ...] | None:
    """First exact adjacent duplicated n-gram in ``text`` (case-folded).

    ``в гости в гости`` -> ``("в", "гости")``; ``он он`` -> ``("он",)``.
    Returns ``None`` when no exact adjacent duplicate exists. Words are
    tokenized as letter/digit runs; punctuation and whitespace are
    irrelevant. Scans n-grams of length 1..4 (a 4-gram repeat is already a
    large block of verbatim text; longer duplicates are rarer and are left
    to semantic verification rather than guessed).
    """
    words = re.findall(r"[А-Яа-яЁёA-Za-z0-9'-]+", (text or "").casefold())
    for n in range(1, 5):
        for i in range(len(words) - 2 * n + 1):
            left = words[i : i + n]
            right = words[i + n : i + 2 * n]
            if left == right:
                return tuple(left)
    return None


def _is_entity_context_issue(issue: Mapping[str, Any], entity_pids: Collection[str]) -> bool:
    """CRITICAL rule (§5.3): chapter entity relations are never Tier A.

    True when the issue's PID is an anchor/alias PID of the chapter entity
    context (any claim — verified or candidate), or when its note/excerpt
    references chapter entity facts. Presence of verified anchor/alias
    spans does NOT promote the relation to Tier A.
    """
    pid = issue.get("id")
    if pid in entity_pids:
        return True
    text = " ".join(
        str(issue.get(key, "")).lower() for key in ("note", "excerpt")
    )
    return any(marker in text for marker in _ENTITY_CONTEXT_NOTE_MARKERS)


def _note_has_numeric_hint(issue: Mapping[str, Any]) -> bool:
    text = " ".join(str(issue.get(key, "")) for key in ("note", "excerpt"))
    return bool(_NOTE_NUMERIC_HINT_RE.search(text))


# --- explicit quoted strings (current-source name/string/object facts) ---
#
# Narrow deterministic contract (RV t_ac2fb507 fix): an explicit quoted
# string in the CURRENT source pair (double/single quotes or RU guillemets)
# that is preserved verbatim in the translation refutes a
# changed_fact/addition/omission claim about that string. A quoted string
# that is NOT preserved verbatim is a semantic edge (the translator may
# legitimately translate or transliterate it — e.g. "STOP" -> «СТОП»), so it
# is never CONFIRMED; unquoted names/objects are out of contract entirely
# (they need entity/lexicon knowledge — B1.2/B2). When no quoted string
# fact is provable the issue stays TIER_B (§5.1 fail-safe).

_QUOTED_STRING_RES = (
    re.compile(r'"([^"\n]{1,120})"'),          # "..."
    re.compile(r"'([^'\n]{1,120})'"),          # '...'
    re.compile(r"\u00ab([^\u00bb\n]{1,120})\u00bb"),  # «...»
)


def _quoted_strings(text: str) -> frozenset:
    """Case-folded quoted-string contents found in ``text`` (deduplicated).

    Only explicitly quoted spans count (double/single quotes or RU
    guillemets); the surrounding quote characters are stripped and the
    content is case-folded for the verbatim-preservation comparison.
    """
    result: set = set()
    for res in _QUOTED_STRING_RES:
        for match in res.finditer(text or ""):
            value = re.sub(r"\s+", " ", match.group(1)).strip().casefold()
            if value:
                result.add(value)
    return frozenset(result)


def _note_has_string_hint(issue: Mapping[str, Any]) -> bool:
    """True when the issue's note/excerpt itself references a quoted string.

    Mirrors ``_note_has_numeric_hint``: a string-fact verdict is only
    attempted when the auditor explicitly quoted the content, so a
    coincidental quote in another finding never triggers the filter.
    """
    text = " ".join(str(issue.get(key, "")) for key in ("note", "excerpt"))
    return any(char in text for char in ('"', "'", "\u00ab", "\u00bb"))


def _source_gender(text: str) -> str | None:
    male = bool(_EN_MALE_RE.search(text))
    female = bool(_EN_FEMALE_RE.search(text))
    if male and not female:
        return "male"
    if female and not male:
        return "female"
    return None  # mixed or absent -> cannot decide deterministically


def _translation_gender(text: str) -> str | None:
    male = bool(_RU_MALE_RE.search(text))
    female = bool(_RU_FEMALE_RE.search(text))
    if male and not female:
        return "male"
    if female and not male:
        return "female"
    return None


@dataclass(frozen=True)
class FilteredIssue:
    """Verdict for one audit issue after the Tier A hard filters."""

    issue: Mapping[str, Any]
    verdict: str
    filter_name: str
    reason: str


def _filter_one(
    issue: Mapping[str, Any],
    *,
    source_text: str,
    translation_text: str,
    source_pids: Collection[str],
    chunk_pids: Collection[str] | None,
    allowed_categories: Collection[str],
    entity_pids: Collection[str],
) -> FilteredIssue:
    # 1. Structure: PID missing/outside chunk, invalid category/severity/
    #    confidence -> REJECT (card scope item 4).
    pid = issue.get("id")
    if not isinstance(pid, str) or not pid:
        return FilteredIssue(issue, REJECTED, "structure", "missing or malformed issue id")
    if chunk_pids is not None and pid not in chunk_pids:
        return FilteredIssue(issue, REJECTED, "structure", f"pid {pid} outside the current chunk")
    if pid not in source_pids:
        return FilteredIssue(issue, REJECTED, "structure", f"pid {pid} missing from source")
    category = issue.get("category")
    if category not in allowed_categories:
        return FilteredIssue(issue, REJECTED, "structure", f"invalid category {category!r}")
    severity = issue.get("severity")
    if severity not in B1_SEVERITIES:
        return FilteredIssue(issue, REJECTED, "structure", f"invalid severity {severity!r}")
    confidence = issue.get("confidence")
    if confidence not in B1_CONFIDENCES:
        return FilteredIssue(issue, REJECTED, "structure", f"invalid confidence {confidence!r}")

    # 2. CRITICAL (§5.3): chapter entity context is NEVER Tier A. Checked
    #    before every confirming filter so a bike=motorcycle issue can never
    #    be confirmed as a numeric/duplicate fact.
    if _is_entity_context_issue(issue, entity_pids):
        return FilteredIssue(
            issue,
            TIER_B,
            "entity_context",
            "finding depends on chapter entity context — always Tier B (§5.3)",
        )

    # 3. Exact adjacent duplicate -> CONFIRMED (card scope item 1).
    duplicate = find_adjacent_duplicate(translation_text)
    if duplicate is not None:
        note = str(issue.get("note", "")).lower()
        excerpt = str(issue.get("excerpt", "")).lower()
        phrase = " ".join(duplicate)
        if category == "addition" or phrase in note + excerpt or any(
            kw in note for kw in ("дубл", "дважд", "повтор", "duplicate", "twice", "два раза")
        ):
            return FilteredIssue(
                issue,
                CONFIRMED,
                "adjacent_duplicate",
                f"exact adjacent duplicate in translation: '{phrase}'",
            )

    # 4. Numbers/time vs the source (card scope items 2-3). POSITIONAL
    #    comparison in text order (RV t_ac2fb507 fix): identical values in
    #    identical order -> REJECTED (FP); the same values in a different
    #    order -> TIER_B (which value belongs to which fact cannot be proven
    #    deterministically — never REJECTED, never CONFIRMED); genuinely
    #    different values -> CONFIRMED. Only for categories where a numeric
    #    claim is plausible, and only when the issue itself references
    #    numeric content (so a coincidental digit in a note never rejects a
    #    genuine semantic finding).
    if category in _NUMERIC_CATEGORIES and _note_has_numeric_hint(issue):
        src_times, src_numbers = _ordered_numeric_values(source_text, "en")
        trn_times, trn_numbers = _ordered_numeric_values(translation_text, "ru")
        src_has = bool(src_times or src_numbers)
        trn_has = bool(trn_times or trn_numbers)
        if src_has or trn_has:
            if src_times == trn_times and src_numbers == trn_numbers:
                return FilteredIssue(
                    issue,
                    REJECTED,
                    "number_time",
                    "source and translation carry the same numeric/time values in the same order",
                )
            if (
                sorted(src_times) == sorted(trn_times)
                and sorted(src_numbers) == sorted(trn_numbers)
            ):
                return FilteredIssue(
                    issue,
                    TIER_B,
                    "number_time",
                    "same numeric/time values in a different order — "
                    "per-fact correspondence is not deterministically provable",
                )
            return FilteredIssue(
                issue,
                CONFIRMED,
                "number_time",
                "source and translation carry different numeric/time values",
            )

    # 5. Direct current-source explicit string fact (§5.1 names/strings,
    #    card scope item 3). Narrow deterministic contract: an explicitly
    #    quoted string in the CURRENT source pair that is preserved verbatim
    #    in the translation refutes a changed_fact/addition/omission claim
    #    about that string (REJECTED). A quoted string that is NOT preserved
    #    verbatim is a semantic edge (the translator may legitimately
    #    translate or transliterate it) -> TIER_B, never CONFIRMED. Unquoted
    #    names/objects are out of contract (need entity/lexicon knowledge).
    if category in _STRING_CATEGORIES and _note_has_string_hint(issue):
        src_strings = _quoted_strings(source_text)
        if src_strings and src_strings <= _quoted_strings(translation_text):
            return FilteredIssue(
                issue,
                REJECTED,
                "explicit_string",
                "every explicitly quoted source string is preserved verbatim in the translation",
            )

    # 6. Direct current-source gender fact (card scope item 3, acceptance:
    #    nurse-issue with explicit source fact "Rich male" -> REJECTED).
    #    Only refutes; never confirms gender from heuristics (invented
    #    gender without explicit traces stays Tier B, §5.1).
    if category in _GENDER_CATEGORIES:
        src_gender = _source_gender(source_text)
        trn_gender = _translation_gender(translation_text)
        if src_gender is not None and src_gender == trn_gender:
            return FilteredIssue(
                issue,
                REJECTED,
                "source_gender",
                f"translation follows the explicit current-source gender fact ({src_gender})",
            )

    # 7. Default: semantic verification required.
    return FilteredIssue(
        issue,
        TIER_B,
        "semantic",
        "no Tier A hard filter decides this finding — semantic verification required",
    )


def apply_hard_filters(
    issues: Sequence[Mapping[str, Any]],
    *,
    source: Mapping[str, str],
    translation: Mapping[str, str],
    chunk_pids: Collection[str] | None = None,
    allowed_categories: Collection[str] = B1_AUDIT_CATEGORIES,
    entity_context: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> Tuple[FilteredIssue, ...]:
    """Classify every audit issue as CONFIRMED / REJECTED / TIER_B.

    ``issues`` are the B1 findings (``id/category/severity/confidence/note/
    excerpt/_debug``); ``source``/``translation`` map PID -> text for the
    chapter (or current chunk). ``chunk_pids`` restricts valid PIDs when the
    caller audits one chunk at a time. ``entity_context`` is the §8.3
    chapter entity context (list of ``{"entity": ..., "claims": [...]}``
    dicts, or a raw collection of PIDs) — any issue whose PID is an
    anchor/alias PID of any claim is forced to TIER_B. Returns one
    ``FilteredIssue`` per input issue, in input order. Pure: no model calls,
    no disk I/O.
    """
    entity_pids: set = set()
    if entity_context is not None:
        if isinstance(entity_context, Mapping):
            # §8.3 shape: iterable of {"entity", "claims": [...]}.
            entities = entity_context.get("entities", ())
        else:
            entities = entity_context
        for entry in entities:
            if isinstance(entry, str):
                entity_pids.add(entry)
                continue
            for claim in entry.get("claims", ()) if isinstance(entry, Mapping) else ():
                for key in ("evidence", "evidence_windows"):
                    value = claim.get(key, ())
                    if isinstance(value, (list, tuple)):
                        for item in value:
                            if isinstance(item, (list, tuple)):
                                entity_pids.update(str(p) for p in item)
                            else:
                                entity_pids.add(str(item))
                    elif value:
                        entity_pids.add(str(value))
    source_pids = frozenset(source)
    results = []
    for issue in issues:
        pid = issue.get("id")
        results.append(
            _filter_one(
                issue,
                source_text=str(source.get(pid, "")) if isinstance(pid, str) else "",
                translation_text=str(translation.get(pid, "")) if isinstance(pid, str) else "",
                source_pids=source_pids,
                chunk_pids=chunk_pids,
                allowed_categories=frozenset(allowed_categories),
                entity_pids=entity_pids,
            )
        )
    return tuple(results)
