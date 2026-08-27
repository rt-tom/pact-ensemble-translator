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

try:
    from pact_v4.runtime.book_memory_policy import BOOK_MEMORY_POLICY_VERSION

except Exception:
    BOOK_MEMORY_POLICY_VERSION = "book-memory-policy/v1"

__all__ = ["render_bible_section", "extract_narrator_gender"]
CHAPTER_INDEX_V2_SCHEMA = "pact-v4-chapter-index/v2"
BOOK_MEMORY_V2_SCHEMA = "pact-v4-book-memory/v2"


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


def _seed_facts(book_memory: Mapping) -> List[Mapping]:
    """Facts marked ``seed: true`` — the global immutable seed facts.

    The clean book_memory (owner-approved seed, 2026-08-14) marks its
    minimal global facts with an explicit ``seed`` flag. These are the
    ONLY facts rendered when no per-chapter index entry exists: they are
    global world knowledge (e.g. ``Blake's vehicle is a motorcycle``) that
    must not leak future chapters, but are safe for every chapter.
    """
    facts = book_memory.get("facts")
    if not isinstance(facts, list):
        return []
    return [
        entry for entry in facts
        if isinstance(entry, Mapping) and entry.get("seed") is True
    ]


def _render_seed_bible(book_memory: Any) -> str:
    """Fail-soft minimum render: narrator + seed facts (NEVER a full dump).

    Replaces the pre-2026-08-14 legacy full-memory fallback (owner decision
    P0, future-leakage fix): when no deterministic per-chapter index entry
    exists the bible must NOT render the whole book_memory (which, after a
    long book run, contains facts from chapters far in the future). Only the
    narrator (gender) and the explicit ``seed: true`` global facts are safe
    to show for an unknown chapter.
    """
    if not isinstance(book_memory, Mapping):
        return ""
    narrator = extract_narrator_gender(book_memory)
    seed_facts = _seed_facts(book_memory)
    if not narrator and not seed_facts:
        return ""
    parts: List[str] = ["BIBLE:"]
    if narrator:
        parts.append(f"  - Narrator: {narrator}")
    if seed_facts:
        parts.append("  - Seed facts:")
        for fact in seed_facts:
            text = (
                _norm_str(fact.get("text", ""))
                or _norm_str(fact.get("fact", ""))
                or _norm_str(fact.get("description", ""))
            )
            if text:
                parts.append(f"  * {text}")
    return "\n".join(parts) + "\n"


def render_bible_section(
    chapter_id: Any = None,
    chapter_index: Any = None,
    book_memory: Any = None,
) -> str:
    """Render the book memory into a ``BIBLE:`` text block for prompts.

    V4.1 A2 (plan §5.2): the primary API is chapter-based —
    ``render_bible_section(chapter_id, chapter_index, book_memory)``. The
    bible is rendered from the deterministic per-chapter entry
    (``chapter_index.json``: ``{characters, facts, address}``) instead of
    the legacy full-memory dump with "first N" caps — the index is already
    chapter-filtered, so no caps apply. The narrator (gender) is always
    included (fail-closed).

    Causal-memory invariant (owner decision 2026-08-14, P0 future-leakage
    fix): when the chapter has NO per-chapter index entry (missing
    ``chapter_index.json`` or an unknown ``chapter_id``) the renderer fails
    SOFT to the minimum — narrator + explicit ``seed: true`` global facts —
    and NEVER falls back to a full book_memory dump. A full dump would
    expose facts from chapters later in the book to an early chapter's
    translation prompt (confirmed leakage: facts from chapters 46/60/100/112/
    148 were rendered into the Bonds 1.1-1.3 prompts when ``chapter_index``
    was not built).

    ``render_bible_section(book_memory)`` (a Mapping first argument, or
    ``None``) keeps the same fail-soft seed render — the legacy full-memory
    form is gone.

    Returns an empty string when there is no renderable content (the
    caller omits the section entirely). The output is deterministic for
    the same input — no set iteration without sorting, no randomness.
    """
    # Legacy call form: render_bible_section(book_memory) — a Mapping passed
    # POSITIONALLY as the first argument. A2 review fix (RV, commit 4ab250b):
    # the KEYWORD form render_bible_section(book_memory=m) (chapter_id=None)
    # must preserve the explicit book_memory instead of overwriting it with
    # chapter_id (None). Positional Mapping and explicit keyword now behave
    # identically — both render the fail-soft seed bible, never a full dump.
    if chapter_id is None or isinstance(chapter_id, Mapping):
        if isinstance(chapter_id, Mapping):
            book_memory = chapter_id
        return _render_seed_bible(book_memory)

    # v2 metadata check: BOTH $schema and $book_memory_policy_version must be present and exact (finding 2 refinement)
    # No alias fallback ("schema" / "policy_version" etc.) — aliases fail soft. Missing/unknown of EITHER fails soft.
    if isinstance(chapter_index, Mapping):
        schema = chapter_index.get("$schema")
        policy_ver = chapter_index.get("$book_memory_policy_version")
        if schema != CHAPTER_INDEX_V2_SCHEMA or policy_ver != BOOK_MEMORY_POLICY_VERSION:
            return _render_seed_bible(book_memory)
    entry = None
    if isinstance(chapter_index, Mapping):
        entry = chapter_index.get(chapter_id)
    if entry is None or not isinstance(entry, Mapping):
        return _render_seed_bible(book_memory)
    return _render_chapter_entry(entry, book_memory)


def _render_chapter_entry(entry: Mapping, book_memory: Any) -> str:
    """Render a per-chapter index entry (narrator always; no caps).

    Causal-memory invariant (P0 owner decision 2026-08-14, RV finding 1):
    the character lines are rendered ONLY from data present in the
    chapter-scoped entry — never enriched with gender/role attributes read
    from the full accumulated ``book_memory``. The chapter index carries
    the names that were visible in THIS chapter; attributes that were
    learned in a LATER chapter (e.g. role ``future-only``) must not leak
    into an early chapter's prompt. A chapter-scoped entry item may carry
    an explicit attrs snapshot (``{name, gender, role}``) — those attrs
    are rendered from the entry itself, still never from book_memory.

    The explicit ``seed: true`` facts from book_memory are ALWAYS included
    in addition to the entry's own facts (deduplicated by text): they are
    global immutable world knowledge (owner decision 2026-08-14) that must
    reach every chapter's prompt even when the index entry was built before
    the seed fact existed. The entry's facts are chapter-scoped; the seed
    facts are the causal-memory floor — never a full dump.
    """
    if not isinstance(book_memory, Mapping):
        book_memory = {}
    narrator = extract_narrator_gender(book_memory)

    characters = entry.get("characters") or []
    named_entities = entry.get("named_entities") or []
    terms = entry.get("terms") or []
    facts = list(entry.get("facts") or [])
    address = entry.get("address") or []

    # Seed facts always render (fail-closed global knowledge), deduplicated
    # against the entry's facts by text so an index that already carries a
    # seed fact does not repeat it.
    seed_texts = {
        _norm_str(f.get("text", "")) or _norm_str(f.get("fact", ""))
        for f in _seed_facts(book_memory)
        if _norm_str(f.get("text", "")) or _norm_str(f.get("fact", ""))
    }
    facts = list(dict.fromkeys(facts))  # de-dup entry facts, keep order
    for seed_text in sorted(seed_texts):
        if seed_text not in facts:
            facts.append(seed_text)

    if not narrator and not characters and not named_entities and not terms and not facts and not address:
        return ""

    parts: List[str] = ["BIBLE:"]
    if narrator:
        parts.append(f"  - Narrator: {narrator}")
    if characters:
        parts.append("  - Characters:")
        for item in characters:
            line = _render_character_line(item)
            if line:
                parts.append(line)
    if named_entities:
        parts.append("  - Named entities:")
        for item in named_entities:
            line = _render_character_line(item) if isinstance(item, Mapping) else f"  * {_norm_str(item)}" if _norm_str(item) else ""
            if line:
                parts.append(line)
    if terms:
        parts.append("  - Terms:")
        for term in terms:
            t = _norm_str(term)
            if t:
                parts.append(f"  * {t}")
    if facts:
        parts.append("  - Facts:")
        for fact in facts:
            parts.append(f"  * {fact}")
    if address:
        parts.append("  - Address register:")
        for item in address:
            parts.append(f"  * {item}")
    return "\n".join(parts) + "\n"


def _render_character_line(item: Any) -> str:
    """Render one character line from a chapter-scoped index entry item.

    Causal-memory invariant (RV finding 1, 2026-08-14): the line is built
    ONLY from the entry item itself — a plain name string, or an explicit
    attrs snapshot ``{name, gender, role}`` carried by the entry. The full
    ``book_memory`` is NEVER consulted here: an attribute learned in a
    future chapter must not leak into an earlier chapter's prompt.
    """
    if isinstance(item, Mapping):
        name = (
            _norm_str(item.get("name", ""))
            or _norm_str(item.get("source", ""))
            or _norm_str(item.get("english", ""))
        )
        if not name:
            return ""
        gender = _norm_str(item.get("gender", ""))
        role = _norm_str(item.get("role", "")) or _norm_str(item.get("description", ""))
        parts = [name]
        if gender:
            parts.append(gender)
        if role:
            parts.append(role)
        return f"  * {', '.join(parts)}"
    name = _norm_str(item)
    if not name:
        return ""
    return f"  * {name}"
