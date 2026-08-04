"""B7: render book_memory (the bible) into a prompt section.

The bible is the v4 ``book_memory.json`` content (V4_MVP_SPEC_RU.md §6):
characters, facts, address register, POV/narrator gender. This module
turns it into a deterministic text block that is appended to generation,
fidelity and audit prompts so the model actually sees the bible facts
instead of merely hashing them for cache identity.

Pure, stateless: no model calls, no disk I/O. The runner loads the JSON
and passes the parsed structure in.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Optional

__all__ = ["render_bible_section", "extract_narrator_gender"]

_MAX_CHARACTERS = 20
_MAX_FACTS = 30
_MAX_ADDRESS = 10


def _norm_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def extract_narrator_gender(book_memory: Any) -> Optional[str]:
    """Extract ``pov.gender`` from the book memory.

    Returns ``"male"``, ``"female"``, or ``None`` (unknown / absent).
    Tolerant of both the flat ``{"pov": {"gender": ...}}`` shape and the
    legacy ``{"narrator_gender": ...}`` top-level key.
    """
    if not isinstance(book_memory, Mapping):
        return None
    pov = book_memory.get("pov")
    if isinstance(pov, Mapping):
        gender = _norm_str(pov.get("gender", ""))
        if gender:
            folded = gender.casefold()
            if folded in ("male", "m", "man", "мужской", "м"):
                return "male"
            if folded in ("female", "f", "woman", "женский", "ж"):
                return "female"
            return folded
    legacy = _norm_str(book_memory.get("narrator_gender", ""))
    if legacy:
        folded = legacy.casefold()
        if folded in ("male", "m", "man", "мужской", "м"):
            return "male"
        if folded in ("female", "f", "woman", "женский", "ж"):
            return "female"
        return folded
    return None


# Note: the single-letter aliases ``"м"`` / ``"ж"`` are accepted because
# the input here is a settings-file value, never a text fragment
# extracted from a translation. A 1-character Cyrillic letter is fine
# as a configuration value; if we ever extract gender from a free-form
# text, this mapping must be tightened.


def _render_characters(book_memory: Mapping) -> List[str]:
    characters = book_memory.get("characters")
    if not characters:
        return []
    lines: List[str] = []
    if isinstance(characters, Mapping):
        items = list(characters.items())
    elif isinstance(characters, list):
        items = []
        for entry in characters:
            if isinstance(entry, Mapping):
                name = (
                    _norm_str(entry.get("name", ""))
                    or _norm_str(entry.get("source", ""))
                    or _norm_str(entry.get("english", ""))
                )
                if name:
                    items.append((name, entry))
    else:
        return []
    for name, attrs in items[:_MAX_CHARACTERS]:
        if isinstance(attrs, Mapping):
            gender = _norm_str(attrs.get("gender", ""))
            role = _norm_str(attrs.get("role", "")) or _norm_str(attrs.get("description", ""))
            parts = [name]
            if gender:
                parts.append(gender)
            if role:
                parts.append(role)
            lines.append(f"  * {', '.join(parts)}")
        else:
            lines.append(f"  * {name}")
    return lines


def _render_facts(book_memory: Mapping) -> List[str]:
    facts = book_memory.get("facts")
    if not facts:
        return []
    lines: List[str] = []
    if isinstance(facts, list):
        for entry in facts[:_MAX_FACTS]:
            if isinstance(entry, Mapping):
                text = (
                    _norm_str(entry.get("text", ""))
                    or _norm_str(entry.get("fact", ""))
                    or _norm_str(entry.get("description", ""))
                )
                if text:
                    lines.append(f"  * {text}")
            elif isinstance(entry, str) and entry.strip():
                lines.append(f"  * {entry.strip()}")
    elif isinstance(facts, Mapping):
        for key, value in list(facts.items())[:_MAX_FACTS]:
            if isinstance(value, str) and value.strip():
                lines.append(f"  * {key}: {value.strip()}")
            elif isinstance(value, Mapping):
                text = _norm_str(value.get("text", "")) or _norm_str(value.get("description", ""))
                if text:
                    lines.append(f"  * {key}: {text}")
    return lines


def _render_address_register(book_memory: Mapping) -> List[str]:
    address = book_memory.get("address_register")
    if not address:
        return []
    lines: List[str] = []
    if isinstance(address, list):
        for entry in address[:_MAX_ADDRESS]:
            if isinstance(entry, Mapping):
                text = (
                    _norm_str(entry.get("text", ""))
                    or _norm_str(entry.get("rule", ""))
                    or _norm_str(entry.get("pattern", ""))
                )
                if text:
                    lines.append(f"  * {text}")
            elif isinstance(entry, str) and entry.strip():
                lines.append(f"  * {entry.strip()}")
    elif isinstance(address, Mapping):
        for key, value in list(address.items())[:_MAX_ADDRESS]:
            if isinstance(value, str) and value.strip():
                lines.append(f"  * {key}: {value.strip()}")
    return lines


def _count_characters(book_memory: Mapping) -> int:
    characters = book_memory.get("characters")
    if isinstance(characters, Mapping):
        return len(characters)
    if isinstance(characters, list):
        return len(characters)
    return 0


def _count_facts(book_memory: Mapping) -> int:
    facts = book_memory.get("facts")
    if isinstance(facts, (list, Mapping)):
        return len(facts)
    return 0


def _count_address(book_memory: Mapping) -> int:
    address = book_memory.get("address_register")
    if isinstance(address, (list, Mapping)):
        return len(address)
    return 0


def render_bible_section(book_memory: Any) -> str:
    """Render the book memory into a ``BIBLE:`` text block for prompts.

    Returns an empty string when the book memory is empty or has no
    renderable content (the caller omits the section entirely). The
    output is deterministic for the same input — no set iteration without
    sorting, no randomness.

    Each section is capped (``_MAX_CHARACTERS``/``_MAX_FACTS``/
    ``_MAX_ADDRESS``). When the cap truncates, the last line of the
    affected section is replaced with ``(showing first N of M)`` so the
    model knows the bible had more content that was dropped for budget
    reasons — without this hint the model would assume the list was
    complete.
    """
    if not isinstance(book_memory, Mapping):
        return ""

    narrator = extract_narrator_gender(book_memory)
    char_lines = _render_characters(book_memory)
    fact_lines = _render_facts(book_memory)
    address_lines = _render_address_register(book_memory)

    if not narrator and not char_lines and not fact_lines and not address_lines:
        return ""

    char_total = _count_characters(book_memory)
    fact_total = _count_facts(book_memory)
    address_total = _count_address(book_memory)

    parts: List[str] = ["BIBLE:"]
    if narrator:
        parts.append(f"  - Narrator: {narrator}")
    if char_lines:
        parts.append("  - Characters:")
        parts.extend(char_lines)
        if char_total > len(char_lines):
            parts.append(f"  (showing first {len(char_lines)} of {char_total})")
    if fact_lines:
        parts.append("  - Facts:")
        parts.extend(fact_lines)
        if fact_total > len(fact_lines):
            parts.append(f"  (showing first {len(fact_lines)} of {fact_total})")
    if address_lines:
        parts.append("  - Address register:")
        parts.extend(address_lines)
        if address_total > len(address_lines):
            parts.append(f"  (showing first {len(address_lines)} of {address_total})")
    return "\n".join(parts) + "\n"
