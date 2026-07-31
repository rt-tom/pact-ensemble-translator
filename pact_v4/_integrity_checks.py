"""Shared, model-free text-integrity helpers used by more than one v4 phase.

Originally written and tested as private helpers inside
``pact_v4.phase2.cascade`` (Phase 2C's deterministic consistency gate,
per-candidate pre-selection). ``pact_v4.phase3.audit`` (Phase 3B's
deterministic chapter-wide audit layer, post-selection) needs the exact
same checks applied to different input shapes, so they live here as a
public, versioned utility surface instead of being imported across phase
boundaries as another module's implementation details (which would be
undocumented coupling: a rename/refactor of an underscore-prefixed name in
one phase would silently break the other).

Pure, stateless functions only — no model calls, no identity/provenance
concerns, no disk I/O.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Iterable, List

__all__ = [
    "extract_digits",
    "missing_numeric_values",
    "source_term_present",
    "find_mixed_script",
    "target_form_present",
    "combine_glossary_terms",
    "RU_DIGIT_EQUIVALENTS",
]

_URL_OR_EMAIL_RE = re.compile(
    r"(?:https?://|www\.)\S+|"
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    re.I,
)
_SCRIPT_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]*")
_SOURCE_BOUNDARY = r"A-Za-z0-9_"


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_digits(text: str) -> List[str]:
    return re.findall(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])", text)


# Russian digit word equivalents (stems for matching inflected forms)
_RU_DIGIT_EQUIVALENTS: Dict[str, "re.Pattern[str]"] = {
    "0": re.compile(r"\b(?:нол(?:ь|я|ю|ём|е)|нул(?:евой|евая|евое|евых?))\b", re.I),
    "1": re.compile(r"\b(?:один|одна|одно|одну|одного|одной|перв\w*)\b", re.I),
    "2": re.compile(r"\b(?:два|две|двое|двух|двум|двумя|обоих|обеих|двойн)\b", re.I),
    "3": re.compile(r"\b(?:три|трое|тр[её]х|тр[её]м|тремя|тройн)\b", re.I),
    "4": re.compile(r"\b(?:четыре|четыр[её]х|четв[её]рт\w*)\b", re.I),
    "5": re.compile(r"\b(?:пять|пяти|пят\w*)\b", re.I),
    "6": re.compile(r"\b(?:шесть|шести|шест\w*)\b", re.I),
    "7": re.compile(r"\b(?:семь|семи|седьм\w*)\b", re.I),
    "8": re.compile(r"\b(?:восемь|восьми|восьм\w*)\b", re.I),
    "9": re.compile(r"\b(?:девять|девяти|девят\w*)\b", re.I),
    "10": re.compile(r"\b(?:десять|десяти|десят\w*)\b", re.I),
    "11": re.compile(r"\b(?:одиннадцать|одиннадцати|одиннадцат\w*)\b", re.I),
    "12": re.compile(r"\b(?:двенадцать|двенадцати|двенадцат\w*)\b", re.I),
    "13": re.compile(r"\b(?:тринадцать|тринадцати|тринадцат\w*)\b", re.I),
    "14": re.compile(r"\b(?:четырнадцать|четырнадцати|четырнадцат\w*)\b", re.I),
    "15": re.compile(r"\b(?:пятнадцать|пятнадцати|пятнадцат\w*)\b", re.I),
    "16": re.compile(r"\b(?:шестнадцать|шестнадцати|шестнадцат\w*)\b", re.I),
    "17": re.compile(r"\b(?:семнадцать|семнадцати|семнадцат\w*)\b", re.I),
    "18": re.compile(r"\b(?:восемнадцать|восемнадцати|восемнадцат\w*)\b", re.I),
    "19": re.compile(r"\b(?:девятнадцать|девятнадцати|девятнадцат\w*)\b", re.I),
    "20": re.compile(r"\b(?:двадцать|двадцати|двадцат\w*)\b", re.I),
}

# Public alias: pact_v4.phase2.cascade's required-risk-category resolution
# gate needs this exact table too (matching a written-out English number
# word's value against its Russian word form), so it is exposed here rather
# than duplicated — the whole point of this module per its docstring.
RU_DIGIT_EQUIVALENTS = _RU_DIGIT_EQUIVALENTS


def missing_numeric_values(source_values: List[str], target_text: str) -> List[str]:
    target_counts = Counter(extract_digits(target_text))
    missing: List[str] = []
    for value, count in Counter(source_values).items():
        exact_count = target_counts.get(value, 0)
        remaining = max(0, count - exact_count)
        if remaining == 0:
            continue
        pattern = _RU_DIGIT_EQUIVALENTS.get(value)
        word_count = len(pattern.findall(target_text)) if pattern else 0
        remaining = max(0, remaining - word_count)
        missing.extend([value] * remaining)
    return missing


def source_term_present(text: str, source: str) -> bool:
    source = _norm(source)
    if not source:
        return False
    escaped = re.escape(source)
    escaped = escaped.replace(r"\ ", r"\s+")
    escaped = escaped.replace("'", "['’]").replace("’", "['’]")
    flags = 0 if source[:1].isupper() else re.I
    return bool(re.search(
        rf"(?<![{_SOURCE_BOUNDARY}]){escaped}(?![{_SOURCE_BOUNDARY}])",
        text, flags=flags,
    ))


def find_mixed_script(text: str, allow: Iterable[str] = ()) -> List[str]:
    allowed = {_norm(str(item)).casefold() for item in allow if _norm(str(item))}
    cleaned = _URL_OR_EMAIL_RE.sub(" ", text or "")
    result: List[str] = []
    seen: set = set()
    for token in _SCRIPT_TOKEN_RE.findall(cleaned):
        if not re.search(r"[A-Za-z]", token):
            continue
        folded = token.casefold()
        if folded in allowed or folded in seen:
            continue
        seen.add(folded)
        result.append(token)
    return result


_RU_ENDINGS = sorted({
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
    "иях", "ах", "ях", "ой", "ей", "ый", "ий", "ая", "яя", "ую",
    "юю", "ом", "ем", "ым", "им", "ов", "ев", "ам", "ям", "а", "я",
    "у", "ю", "ы", "и", "е", "о", "ь",
}, key=len, reverse=True)


def _ru_core(token: str) -> str:
    token = re.sub(r"[^А-Яа-яЁё]", "", token).casefold().replace("ё", "е")
    if len(token) <= 4:
        return token
    for ending in _RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[:-len(ending)]
    return token


def target_form_present(text: str, expected: str) -> bool:
    expected_core = _norm(expected)
    if not expected_core:
        return False
    folded = text.casefold().replace("ё", "е")
    if expected_core.casefold().replace("ё", "е") in folded:
        return True
    expected_tokens = re.findall(r"[А-Яа-яЁё]+", expected_core)
    target_tokens = [
        token.casefold().replace("ё", "е")
        for token in re.findall(r"[А-Яа-яЁё]+", text)
    ]
    cores = [_ru_core(token) for token in expected_tokens]
    cores = [core for core in cores if len(core) >= 4]
    if not cores:
        return False
    return all(any(token.startswith(core) for token in target_tokens) for core in cores)


def combine_glossary_terms(
    glossary_terms: Iterable[tuple], names: Iterable[tuple]
) -> Dict[str, str]:
    """Merge glossary-term and name pairs into one EN -> RU lookup.

    Shared by both Phase 2C's per-candidate gate and Phase 3B's chapter-wide
    audit — both need the exact same "known EN terms/names must use the
    established RU form" lookup, built from the same two sources.
    """
    combined: Dict[str, str] = {}
    for en_term, ru_term in glossary_terms:
        key = en_term.strip()
        if key and ru_term.strip():
            combined[key] = ru_term.strip()
    for en_name, ru_name in names:
        key = en_name.strip()
        if key and ru_name.strip():
            combined[key] = ru_name.strip()
    return combined
